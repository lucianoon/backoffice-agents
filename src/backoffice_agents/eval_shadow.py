"""Avaliação em sombra da triagem: Jev (real e/ou emulado) contra rótulos humanos.

Mede acurácia por pergunta, calibração (ECE em 10 faixas) da categoria e latência.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import decisions
from .jev import JevClient


@dataclass
class ShadowResult:
    model: str
    n: int = 0
    category_correct: int = 0
    urgency_within_one: int = 0
    needs_human_correct: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    category_bins: list[tuple[float, bool]] = field(default_factory=list)  # (confiança, acertou)
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def category_accuracy(self) -> float:
        return self.category_correct / self.n if self.n else 0.0

    @property
    def urgency_accuracy(self) -> float:
        return self.urgency_within_one / self.n if self.n else 0.0

    @property
    def needs_human_accuracy(self) -> float:
        return self.needs_human_correct / self.n if self.n else 0.0

    @property
    def mean_latency_ms(self) -> float:
        return statistics.fmean(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def ece(self) -> float:
        return expected_calibration_error(self.category_bins)


def expected_calibration_error(pairs: list[tuple[float, bool]], bins: int = 10) -> float:
    if not pairs:
        return 0.0
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for confidence, correct in pairs:
        index = min(bins - 1, int(confidence * bins))
        buckets[index].append((confidence, correct))
    total = len(pairs)
    ece = 0.0
    for bucket in buckets:
        if not bucket:
            continue
        avg_conf = statistics.fmean(c for c, _ in bucket)
        accuracy = sum(1 for _, ok in bucket if ok) / len(bucket)
        ece += (len(bucket) / total) * abs(avg_conf - accuracy)
    return ece


def load_dataset(samples_path: str, labels_path: str) -> list[dict[str, Any]]:
    emails = {e["id"]: e for e in json.loads(Path(samples_path).read_text(encoding="utf-8"))}
    dataset = []
    for line in Path(labels_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row["id"] in emails:
            dataset.append({"email": emails[row["id"]], "labels": row["labels"]})
    return dataset


def run_shadow(client: JevClient, dataset: list[dict[str, Any]], model_label: str) -> ShadowResult:
    result = ShadowResult(model=model_label)
    for row in dataset:
        response = client.ask(decisions.triage_state(row["email"], None), decisions.triage_questions())
        labels = row["labels"]
        category = response.choice("category")
        urgency = response.score("urgency").score
        needs_human = response.noul("needs_human") >= 0.5

        cat_ok = category.choice == labels["category"]
        urg_ok = abs(round(urgency) - int(labels["urgency"])) <= 1
        human_ok = needs_human == bool(labels["needs_human"])

        result.n += 1
        result.category_correct += cat_ok
        result.urgency_within_one += urg_ok
        result.needs_human_correct += human_ok
        result.latencies_ms.append(response.latency_ms)
        result.category_bins.append((category.confidence, cat_ok))
        result.rows.append({
            "id": row["email"]["id"], "expected": labels["category"], "predicted": category.choice,
            "confidence": round(category.confidence, 2), "urgency": round(urgency, 1),
            "needs_human": round(response.noul("needs_human"), 2), "latency_ms": round(response.latency_ms),
        })
    return result
