"""Calibração ao longo do tempo: compara as decisões do Jev com os rótulos humanos gravados.

Agrupa por pergunta e por modelo, para que Jev real e emulador nunca se misturem.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .eval_shadow import expected_calibration_error

TRUE_WORDS = {"true", "sim", "yes", "1"}
FALSE_WORDS = {"false", "não", "nao", "no", "0"}


@dataclass
class CalibrationRow:
    question_id: str
    model: str
    version: str
    n: int
    accuracy: float
    ece: float
    mean_confidence: float


def _judge(decision: dict[str, Any]) -> tuple[bool, float] | None:
    """Devolve (acertou, confiança) para uma decisão rotulada, ou None se o rótulo for inválido."""
    answer, label = decision["answer"], str(decision["human_label"]).strip().lower()
    kind = decision["question_type"]
    if kind == "choice":
        return answer["choice"].lower() == label, float(answer["confidence"])
    if kind == "noul":
        if label in TRUE_WORDS:
            truth = True
        elif label in FALSE_WORDS:
            truth = False
        else:
            return None
        p = float(answer["noul"])
        return (p >= 0.5) == truth, max(p, 1 - p)
    if kind == "score":
        try:
            truth = float(label)
        except ValueError:
            return None
        return abs(round(float(answer["score"])) - truth) <= 1, float(answer.get("confidence", 0.0))
    return None


def calibration_report(decisions: list[dict[str, Any]]) -> list[CalibrationRow]:
    groups: dict[tuple[str, str, str], list[tuple[bool, float]]] = defaultdict(list)
    for d in decisions:
        if not str(d.get("human_label") or "").strip():
            continue
        judged = _judge(d)
        if judged is not None:
            groups[(d["question_id"], d["model"], d.get("version") or "")].append(judged)
    rows = []
    for (question_id, model, version), pairs in sorted(groups.items()):
        rows.append(CalibrationRow(
            question_id=question_id, model=model, version=version, n=len(pairs),
            accuracy=sum(1 for ok, _ in pairs if ok) / len(pairs),
            ece=expected_calibration_error([(c, ok) for ok, c in pairs]),
            mean_confidence=statistics.fmean(c for _, c in pairs),
        ))
    return rows


def format_report(rows: list[CalibrationRow]) -> str:
    if not rows:
        return "Nenhuma decisão rotulada ainda."
    lines = ["pergunta | modelo | versão | n | acurácia | ECE | conf. média"]
    for r in rows:
        lines.append(f"{r.question_id} | {r.model} | {r.version or '-'} | {r.n} | {r.accuracy:.0%} | "
                     f"{r.ece:.3f} | {r.mean_confidence:.2f}")
    return "\n".join(lines)
