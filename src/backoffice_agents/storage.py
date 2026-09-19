"""Persistência e fila de trabalho.

SQLAlchemy Core sobre SQLite (dev) ou Postgres (`DB_URL=postgresql+psycopg://...`). A fila é a
própria tabela `work_items`: `claim_next` reserva um item de forma atômica (FOR UPDATE SKIP LOCKED
no Postgres; transação imediata no SQLite), então vários workers e o poller do Telegram podem
rodar ao mesmo tempo sem processar o mesmo item duas vezes.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine, Row

metadata = MetaData()

work_items = Table(
    "work_items", metadata,
    Column("id", String(128), primary_key=True),
    Column("source", String(32), nullable=False),
    Column("status", String(32), nullable=False, index=True),
    Column("payload", Text, nullable=False),
    Column("state", Text),
    Column("attempts", Integer, nullable=False, default=0, server_default="0"),
    Column("claimed_by", String(128)),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
)

decisions = Table(
    "decisions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("item_id", String(128), nullable=False, index=True),
    Column("stage", String(64), nullable=False),
    Column("question_id", String(64), nullable=False),
    Column("question_type", String(16), nullable=False),
    Column("answer", Text, nullable=False),
    Column("confidence", Float, nullable=False),
    Column("calibrated", Integer, nullable=False),
    Column("model", String(64), nullable=False),
    Column("latency_ms", Float, nullable=False),
    Column("human_label", String(128)),
    Column("human_label_by", String(128)),
    Column("created_at", String(40), nullable=False),
)

approvals = Table(
    "approvals", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("item_id", String(128), nullable=False, index=True),
    Column("kind", String(16), nullable=False),
    Column("action", Text, nullable=False),
    Column("status", String(16), nullable=False, index=True),
    Column("telegram_message_id", String(64)),
    Column("requested_at", String(40), nullable=False),
    Column("decided_at", String(40)),
    Column("decided_by", String(128)),
)

# Toda chamada a modelo (LLM do agente, emulador ou Jev real): base do relatório de custo/latência.
model_calls = Table(
    "model_calls", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("item_id", String(128), nullable=False, index=True),
    Column("kind", String(8), nullable=False),        # llm | jev
    Column("stage", String(64), nullable=False),
    Column("model", String(64), nullable=False),
    Column("input_tokens", Integer, nullable=False, default=0),
    Column("output_tokens", Integer, nullable=False, default=0),
    Column("latency_ms", Float, nullable=False),
    Column("calibrated", Integer, nullable=False, default=1),
    Column("created_at", String(40), nullable=False),
)

CLAIMABLE_APPROVALS = ("approved", "rejected")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class Store:
    def __init__(self, url: str, worker_id: str | None = None) -> None:
        self.url = url
        self.worker_id = worker_id or default_worker_id()
        self.is_sqlite = url.startswith("sqlite")
        if self.is_sqlite and ":memory:" not in url:
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(
            url, future=True, connect_args={"timeout": 30} if self.is_sqlite else {})
        if self.is_sqlite:
            @event.listens_for(self.engine, "connect")
            def _pragmas(dbapi_conn, _record):  # WAL: leitores não bloqueiam o escritor
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.close()
        metadata.create_all(self.engine)
        self._migrate()

    def _migrate(self) -> None:
        """Bancos criados antes de uma coluna existir ganham a coluna (create_all não altera)."""
        inspector = inspect(self.engine)
        existing = {c["name"] for c in inspector.get_columns("work_items")}
        decision_columns = {c["name"] for c in inspector.get_columns("decisions")}
        with self.engine.begin() as conn:
            if "attempts" not in existing:
                conn.execute(text("ALTER TABLE work_items ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"))
            if "claimed_by" not in existing:
                conn.execute(text("ALTER TABLE work_items ADD COLUMN claimed_by VARCHAR(128)"))
            if "human_label_by" not in decision_columns:
                conn.execute(text("ALTER TABLE decisions ADD COLUMN human_label_by VARCHAR(128)"))

    # ---- work items ----
    def upsert_item(self, item_id: str, source: str, status: str, payload: dict[str, Any],
                    state: dict[str, Any] | None = None) -> None:
        now = _now()
        state_json = json.dumps(state, ensure_ascii=False) if state is not None else None
        with self.engine.begin() as conn:
            if conn.execute(select(work_items.c.id).where(work_items.c.id == item_id)).first():
                conn.execute(update(work_items).where(work_items.c.id == item_id)
                             .values(status=status, state=state_json, updated_at=now))
            else:
                conn.execute(work_items.insert().values(
                    id=item_id, source=source, status=status,
                    payload=json.dumps(payload, ensure_ascii=False), state=state_json,
                    attempts=0, created_at=now, updated_at=now))

    def set_item_state(self, item_id: str, status: str, state: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(work_items).where(work_items.c.id == item_id).values(
                status=status, state=json.dumps(state, ensure_ascii=False), updated_at=_now()))

    def increment_attempts(self, item_id: str) -> int:
        with self.engine.begin() as conn:
            conn.execute(update(work_items).where(work_items.c.id == item_id)
                         .values(attempts=work_items.c.attempts + 1))
            row = conn.execute(select(work_items.c.attempts).where(work_items.c.id == item_id)).first()
        return int(row[0]) if row else 0

    def claim_next(self, max_attempts: int, retry_delay_s: float = 0.0,
                   exclude: set[str] | None = None) -> dict[str, Any] | None:
        """Reserva atomicamente o próximo item novo, ou em erro com tentativas restantes.

        Itens em erro só voltam depois de `retry_delay_s` desde a última atualização (backoff
        simples). `exclude` evita repetir no mesmo ciclo um item que acabou de falhar.
        """
        retry_before = (datetime.now(UTC) - timedelta(seconds=retry_delay_s)).isoformat(timespec="seconds")
        retryable = ((work_items.c.status == "error") & (work_items.c.attempts < max_attempts)
                     & (work_items.c.updated_at <= retry_before))
        condition = (work_items.c.status == "new") | retryable
        if exclude:
            condition = condition & work_items.c.id.not_in(list(exclude))
        candidates = select(work_items.c.id).where(condition).order_by(work_items.c.created_at).limit(1)
        if not self.is_sqlite:
            candidates = candidates.with_for_update(skip_locked=True)
        with self.engine.begin() as conn:
            if self.is_sqlite:
                conn.execute(text("BEGIN IMMEDIATE"))  # trava de escrita antes de escolher
            row = conn.execute(candidates).first()
            if row is None:
                return None
            item_id = row[0]
            result = conn.execute(update(work_items).where(
                (work_items.c.id == item_id) & (work_items.c.status.in_(["new", "error"]))
            ).values(status="processing", claimed_by=self.worker_id, updated_at=_now()))
            if result.rowcount != 1:
                return None
        return self.get_item(item_id)

    def list_retryable(self, max_attempts: int) -> list[dict[str, Any]]:
        stmt = select(work_items).where((work_items.c.status == "error")
                                        & (work_items.c.attempts < max_attempts)
                                        ).order_by(work_items.c.created_at)
        with self.engine.connect() as conn:
            return [_item(r) for r in conn.execute(stmt)]

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(work_items).where(work_items.c.id == item_id)).first()
        return _item(row) if row else None

    def list_items(self, status: str | None = None) -> list[dict[str, Any]]:
        stmt = select(work_items).order_by(work_items.c.created_at)
        if status:
            stmt = stmt.where(work_items.c.status == status)
        with self.engine.connect() as conn:
            return [_item(r) for r in conn.execute(stmt)]

    # ---- decisions ----
    def log_decision(self, item_id: str, stage: str, question_id: str, question_type: str,
                     answer: dict[str, Any], confidence: float, calibrated: bool, model: str,
                     latency_ms: float) -> None:
        with self.engine.begin() as conn:
            conn.execute(decisions.insert().values(
                item_id=item_id, stage=stage, question_id=question_id, question_type=question_type,
                answer=json.dumps(answer, ensure_ascii=False), confidence=confidence,
                calibrated=int(calibrated), model=model, latency_ms=latency_ms, created_at=_now()))

    def set_human_label(self, item_id: str, stage: str, question_id: str, label: str,
                        labeled_by: str | None = None) -> int:
        with self.engine.begin() as conn:
            result = conn.execute(update(decisions).where(
                (decisions.c.item_id == item_id) & (decisions.c.stage == stage)
                & (decisions.c.question_id == question_id)
            ).values(human_label=label, human_label_by=labeled_by))
        return result.rowcount

    def list_decisions(self, item_id: str | None = None) -> list[dict[str, Any]]:
        stmt = select(decisions).order_by(decisions.c.id)
        if item_id:
            stmt = stmt.where(decisions.c.item_id == item_id)
        with self.engine.connect() as conn:
            return [dict(r._mapping) | {"answer": json.loads(r._mapping["answer"])}
                    for r in conn.execute(stmt)]

    # ---- model calls (custo e latência) ----
    def log_model_call(self, item_id: str, kind: str, stage: str, model: str, input_tokens: int,
                       output_tokens: int, latency_ms: float, calibrated: bool = True) -> None:
        with self.engine.begin() as conn:
            conn.execute(model_calls.insert().values(
                item_id=item_id, kind=kind, stage=stage, model=model, input_tokens=int(input_tokens),
                output_tokens=int(output_tokens), latency_ms=float(latency_ms),
                calibrated=int(calibrated), created_at=_now()))

    def list_model_calls(self, item_id: str | None = None) -> list[dict[str, Any]]:
        stmt = select(model_calls).order_by(model_calls.c.id)
        if item_id:
            stmt = stmt.where(model_calls.c.item_id == item_id)
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(stmt)]

    # ---- approvals ----
    def create_approval(self, item_id: str, kind: str, action: dict[str, Any],
                        telegram_message_id: str | None) -> int:
        with self.engine.begin() as conn:
            result = conn.execute(approvals.insert().values(
                item_id=item_id, kind=kind, action=json.dumps(action, ensure_ascii=False),
                status="pending", telegram_message_id=telegram_message_id, requested_at=_now()))
        return int(result.inserted_primary_key[0])

    def set_approval_message(self, approval_id: int, telegram_message_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(approvals).where(approvals.c.id == approval_id)
                         .values(telegram_message_id=telegram_message_id))

    def set_approval_status(self, approval_id: int, status: str) -> None:
        """Transições pós-decisão: approved/rejected -> applying -> applied."""
        with self.engine.begin() as conn:
            conn.execute(update(approvals).where(approvals.c.id == approval_id).values(status=status))

    def mark_approval_applied(self, approval_id: int) -> None:
        self.set_approval_status(approval_id, "applied")

    def claim_next_approval(self) -> dict[str, Any] | None:
        """Reserva atomicamente a próxima aprovação decidida (approved/rejected -> applying)."""
        candidates = select(approvals.c.id, approvals.c.status).where(
            approvals.c.status.in_(CLAIMABLE_APPROVALS)).order_by(approvals.c.id).limit(1)
        if not self.is_sqlite:
            candidates = candidates.with_for_update(skip_locked=True)
        with self.engine.begin() as conn:
            if self.is_sqlite:
                conn.execute(text("BEGIN IMMEDIATE"))
            row = conn.execute(candidates).first()
            if row is None:
                return None
            approval_id, decided_status = row[0], row[1]
            result = conn.execute(update(approvals).where(
                (approvals.c.id == approval_id) & (approvals.c.status == decided_status)
            ).values(status="applying"))
            if result.rowcount != 1:
                return None
        approval = self.get_approval(approval_id)
        approval["decided_status"] = decided_status  # o runner precisa saber se foi aprovada
        return approval

    def decide_approval(self, approval_id: int, approved: bool, decided_by: str) -> dict[str, Any] | None:
        with self.engine.begin() as conn:
            conn.execute(update(approvals).where(
                (approvals.c.id == approval_id) & (approvals.c.status == "pending")).values(
                status="approved" if approved else "rejected", decided_at=_now(), decided_by=decided_by))
        return self.get_approval(approval_id)

    def get_approval(self, approval_id: int) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(approvals).where(approvals.c.id == approval_id)).first()
        return _approval(row) if row else None

    def list_approvals(self, status: str = "pending") -> list[dict[str, Any]]:
        stmt = select(approvals).where(approvals.c.status == status).order_by(approvals.c.id)
        with self.engine.connect() as conn:
            return [_approval(r) for r in conn.execute(stmt)]

    def latest_approval_for_item(self, item_id: str) -> dict[str, Any] | None:
        stmt = (select(approvals).where(approvals.c.item_id == item_id)
                .order_by(approvals.c.id.desc()).limit(1))
        with self.engine.connect() as conn:
            row = conn.execute(stmt).first()
        return _approval(row) if row else None


def _item(row: Row) -> dict[str, Any]:
    data = dict(row._mapping)
    data["payload"] = json.loads(data["payload"])
    data["state"] = json.loads(data["state"]) if data["state"] else None
    return data


def _approval(row: Row) -> dict[str, Any]:
    data = dict(row._mapping)
    data["action"] = json.loads(data["action"])
    return data
