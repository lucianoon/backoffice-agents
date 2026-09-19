"""Rotulagem por dois anotadores: exportar lote, medir concordância (kappa de Cohen) e consolidar."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LABEL_FIELDS = ("category", "urgency", "needs_human")


def export_batch(emails: list[dict[str, Any]], out: Path) -> int:
    """Escreve um JSONL com os campos de rótulo vazios, para cada anotador preencher a sua cópia."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for email in emails:
            row = {"id": email["id"], "from_addr": email.get("from_addr", ""),
                   "subject": email.get("subject", ""), "body": email.get("body", ""),
                   "labels": {f: None for f in LABEL_FIELDS}}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(emails)


def load_labels(path: Path) -> dict[str, dict[str, Any]]:
    labels: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            labels[row["id"]] = _normalize(row.get("labels", {}))
    return labels


def _normalize(labels: dict[str, Any]) -> dict[str, Any]:
    out = dict(labels)
    if out.get("urgency") is not None:
        out["urgency"] = int(out["urgency"])
    if isinstance(out.get("needs_human"), str):
        out["needs_human"] = out["needs_human"].strip().lower() in {"true", "sim", "1", "yes"}
    return out


def cohen_kappa(a: list[Any], b: list[Any]) -> float:
    """Concordância corrigida pelo acaso. 1 = perfeita, 0 = igual ao acaso."""
    n = len(a)
    if n == 0:
        return 0.0
    po = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb.get(k, 0) for k in ca) / (n * n)
    if pe == 1.0:
        return 1.0 if po == 1.0 else 0.0
    return (po - pe) / (1 - pe)


@dataclass
class FieldAgreement:
    field: str
    n: int
    exact: float
    kappa: float
    within_one: float | None = None  # só para urgency


@dataclass
class AgreementReport:
    fields: list[FieldAgreement] = field(default_factory=list)
    disagreements: list[dict[str, Any]] = field(default_factory=list)
    only_in_a: list[str] = field(default_factory=list)
    only_in_b: list[str] = field(default_factory=list)


def agreement(labels_a: dict[str, dict[str, Any]], labels_b: dict[str, dict[str, Any]]) -> AgreementReport:
    common = [i for i in labels_a if i in labels_b]
    report = AgreementReport(only_in_a=[i for i in labels_a if i not in labels_b],
                             only_in_b=[i for i in labels_b if i not in labels_a])
    for name in LABEL_FIELDS:
        pairs = [(labels_a[i].get(name), labels_b[i].get(name)) for i in common
                 if labels_a[i].get(name) is not None and labels_b[i].get(name) is not None]
        if not pairs:
            continue
        a, b = [p[0] for p in pairs], [p[1] for p in pairs]
        exact = sum(1 for x, y in pairs if x == y) / len(pairs)
        within = None
        if name == "urgency":
            within = sum(1 for x, y in pairs if abs(int(x) - int(y)) <= 1) / len(pairs)
        report.fields.append(FieldAgreement(name, len(pairs), exact, cohen_kappa(a, b), within))
    for i in common:
        for name in LABEL_FIELDS:
            x, y = labels_a[i].get(name), labels_b[i].get(name)
            if x is not None and y is not None and x != y:
                report.disagreements.append({"id": i, "field": name, "a": x, "b": y})
    return report


def merge(labels_a: dict[str, dict[str, Any]], labels_b: dict[str, dict[str, Any]]
          ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Consolida onde os dois concordam nos três campos; o resto vai para adjudicação."""
    merged, conflicts = [], []
    for item_id in labels_a:
        if item_id not in labels_b:
            continue
        a, b = labels_a[item_id], labels_b[item_id]
        diffs = {f: {"a": a.get(f), "b": b.get(f)} for f in LABEL_FIELDS if a.get(f) != b.get(f)}
        if diffs:
            conflicts.append({"id": item_id, "conflicts": diffs,
                              "agreed": {f: a.get(f) for f in LABEL_FIELDS if f not in diffs}})
        else:
            merged.append({"id": item_id, "labels": {f: a.get(f) for f in LABEL_FIELDS}})
    return merged, conflicts


def write_jsonl(rows: list[dict[str, Any]], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
