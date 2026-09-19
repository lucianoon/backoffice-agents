"""Persistência em SQLite: itens de trabalho, log de decisões do Jev e aprovações."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    state TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    question_id TEXT NOT NULL,
    question_type TEXT NOT NULL,
    answer TEXT NOT NULL,
    confidence REAL NOT NULL,
    calibrated INTEGER NOT NULL,
    model TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    human_label TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    telegram_message_id TEXT,
    requested_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    # ---- work items ----
    def upsert_item(self, item_id: str, source: str, status: str, payload: dict[str, Any],
                    state: dict[str, Any] | None = None) -> None:
        now = _now()
        self._conn.execute(
            """INSERT INTO work_items (id, source, status, payload, state, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status, state=excluded.state,
               updated_at=excluded.updated_at""",
            (item_id, source, status, json.dumps(payload, ensure_ascii=False),
             json.dumps(state, ensure_ascii=False) if state is not None else None, now, now),
        )
        self._conn.commit()

    def set_item_state(self, item_id: str, status: str, state: dict[str, Any]) -> None:
        self._conn.execute("UPDATE work_items SET status=?, state=?, updated_at=? WHERE id=?",
                           (status, json.dumps(state, ensure_ascii=False), _now(), item_id))
        self._conn.commit()

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM work_items WHERE id=?", (item_id,)).fetchone()
        return _item(row) if row else None

    def list_items(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self._conn.execute("SELECT * FROM work_items WHERE status=? ORDER BY created_at",
                                      (status,)).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM work_items ORDER BY created_at").fetchall()
        return [_item(r) for r in rows]

    # ---- decisions ----
    def log_decision(self, item_id: str, stage: str, question_id: str, question_type: str,
                     answer: dict[str, Any], confidence: float, calibrated: bool, model: str,
                     latency_ms: float) -> None:
        self._conn.execute(
            """INSERT INTO decisions (item_id, stage, question_id, question_type, answer, confidence,
               calibrated, model, latency_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item_id, stage, question_id, question_type, json.dumps(answer, ensure_ascii=False),
             confidence, int(calibrated), model, latency_ms, _now()),
        )
        self._conn.commit()

    def set_human_label(self, item_id: str, stage: str, question_id: str, label: str) -> int:
        cur = self._conn.execute(
            "UPDATE decisions SET human_label=? WHERE item_id=? AND stage=? AND question_id=?",
            (label, item_id, stage, question_id))
        self._conn.commit()
        return cur.rowcount

    def list_decisions(self, item_id: str | None = None) -> list[dict[str, Any]]:
        if item_id:
            rows = self._conn.execute("SELECT * FROM decisions WHERE item_id=? ORDER BY id",
                                      (item_id,)).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM decisions ORDER BY id").fetchall()
        return [dict(r) | {"answer": json.loads(r["answer"])} for r in rows]

    # ---- approvals ----
    def create_approval(self, item_id: str, kind: str, action: dict[str, Any],
                        telegram_message_id: str | None) -> int:
        cur = self._conn.execute(
            """INSERT INTO approvals (item_id, kind, action, status, telegram_message_id, requested_at)
               VALUES (?, ?, ?, 'pending', ?, ?)""",
            (item_id, kind, json.dumps(action, ensure_ascii=False), telegram_message_id, _now()))
        self._conn.commit()
        return int(cur.lastrowid)

    def set_approval_message(self, approval_id: int, telegram_message_id: str) -> None:
        self._conn.execute("UPDATE approvals SET telegram_message_id=? WHERE id=?",
                           (telegram_message_id, approval_id))
        self._conn.commit()

    def mark_approval_applied(self, approval_id: int) -> None:
        self._conn.execute("UPDATE approvals SET status='applied' WHERE id=?", (approval_id,))
        self._conn.commit()

    def decide_approval(self, approval_id: int, approved: bool, decided_by: str) -> dict[str, Any] | None:
        self._conn.execute(
            "UPDATE approvals SET status=?, decided_at=?, decided_by=? WHERE id=? AND status='pending'",
            ("approved" if approved else "rejected", _now(), decided_by, approval_id))
        self._conn.commit()
        return self.get_approval(approval_id)

    def get_approval(self, approval_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return _approval(row) if row else None

    def list_approvals(self, status: str = "pending") -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM approvals WHERE status=? ORDER BY id", (status,)).fetchall()
        return [_approval(r) for r in rows]

    def latest_approval_for_item(self, item_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM approvals WHERE item_id=? ORDER BY id DESC LIMIT 1",
                                 (item_id,)).fetchone()
        return _approval(row) if row else None


def _item(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["payload"] = json.loads(data["payload"])
    data["state"] = json.loads(data["state"]) if data["state"] else None
    return data


def _approval(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["action"] = json.loads(data["action"])
    return data
