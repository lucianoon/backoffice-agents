"""Avaliação em sombra da triagem: Jev (real e/ou emulado) contra rótulos humanos.

Mede acurácia por pergunta, calibração (ECE em 10 faixas) da categoria, latência e sugere
limiares de confiança por categoria para a política de roteamento.

Pergunta sem resposta válida (chave ausente, tipo errado, valor não numérico) é fail-closed: conta
como erro na acurácia, nunca como 0.5 inventado, e aparece à parte em `missing`/`missing_rate`.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import decisions
from .jev import ChoiceAnswer, JevClient, JevResponse, ScoreAnswer
from .jev.models import is_finite_number, probability, valid_noul
from .privacy import Pseudonymizer
from .tenant import Tenant


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
    missing: dict[str, int] = field(default_factory=dict)  # pergunta -> linhas sem resposta válida

    @property
    def missing_total(self) -> int:
        return sum(self.missing.values())

    @property
    def missing_rate(self) -> float:
        """Respostas ausentes / respostas esperadas (3 perguntas avaliadas por linha)."""
        return self.missing_total / (self.n * len(TRIAGE_KEYS)) if self.n else 0.0

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


MISSING = "sem resposta"
TRIAGE_KEYS = ("category", "urgency", "needs_human")


def _choice(response: JevResponse, key: str) -> ChoiceAnswer | None:
    answer = response.answers.get(key)
    if isinstance(answer, ChoiceAnswer) and probability(answer.confidence) is not None:
        return answer
    return None


def _score(response: JevResponse, key: str) -> float | None:
    answer = response.answers.get(key)
    return float(answer.score) if isinstance(answer, ScoreAnswer) and is_finite_number(answer.score) else None


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


def load_emails(paths: list[str]) -> list[dict[str, Any]]:
    emails: list[dict[str, Any]] = []
    for path in paths:
        p = Path(path)
        if p.exists():
            emails.extend(json.loads(p.read_text(encoding="utf-8")))
    return emails


def load_dataset(samples_paths: list[str], labels_path: str) -> list[dict[str, Any]]:
    emails = {e["id"]: e for e in load_emails(samples_paths)}
    dataset = []
    for line in Path(labels_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row["id"] in emails:
            dataset.append({"email": emails[row["id"]], "labels": row["labels"]})
    return dataset


def run_shadow(client: JevClient, dataset: list[dict[str, Any]], model_label: str,
               anonymize: bool = True, tenant: Tenant | None = None) -> ShadowResult:
    """Mesmo estado que a produção envia (pseudonimizado por padrão), para medir o que vai ao ar."""
    result = ShadowResult(model=model_label)
    for row in dataset:
        state = decisions.triage_state(row["email"], None)
        if anonymize:
            state = Pseudonymizer([row["email"].get("from_name", "")]).apply(state)
        response = client.ask(state, decisions.triage_questions(tenant))
        labels = row["labels"]
        category = _choice(response, "category")
        urgency = _score(response, "urgency")
        needs_human = valid_noul(response, "needs_human")
        for key, value in zip(TRIAGE_KEYS, (category, urgency, needs_human), strict=True):
            if value is None:
                result.missing[key] = result.missing.get(key, 0) + 1

        # sem resposta = erro (fail-closed), nunca um palpite
        cat_ok = category is not None and category.choice == labels["category"]
        urg_ok = urgency is not None and abs(round(urgency) - int(labels["urgency"])) <= 1
        human_ok = needs_human is not None and (needs_human >= 0.5) == bool(labels["needs_human"])

        result.n += 1
        result.category_correct += cat_ok
        result.urgency_within_one += urg_ok
        result.needs_human_correct += human_ok
        result.latencies_ms.append(response.latency_ms)
        if category is not None:  # ECE só sobre o respondido; a ausência já pesa na acurácia
            result.category_bins.append((category.confidence, cat_ok))
        result.rows.append({
            "id": row["email"]["id"], "expected": labels["category"],
            "predicted": category.choice if category is not None else None,
            "confidence": round(category.confidence, 2) if category is not None else None,
            "urgency": round(urgency, 1) if urgency is not None else None,
            "needs_human": round(needs_human, 2) if needs_human is not None else None,
            "latency_ms": round(response.latency_ms),
        })
    return result


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


@dataclass
class StageResult:
    """Avaliação em sombra do gate ou da verificação: métricas por pergunta."""
    stage: str
    n: int
    metrics: dict[str, dict[str, float]]
    rows: list[dict[str, Any]]
    mean_latency_ms: float

    @property
    def missing_total(self) -> int:
        return sum(int(m.get("missing", 0)) for m in self.metrics.values())

    @property
    def missing_rate(self) -> float:
        """Respostas ausentes / respostas esperadas, somando todas as perguntas do estágio."""
        expected = self.n * len(self.metrics)
        return self.missing_total / expected if expected else 0.0


def _noul_metrics(pairs: list[tuple[float | None, bool]]) -> dict[str, float]:
    """pairs = (probabilidade prevista, ou None se sem resposta; rótulo verdadeiro).

    Sem resposta conta como erro na acurácia; o ECE é medido só sobre as respondidas.
    """
    if not pairs:
        return {"n": 0, "accuracy": 0.0, "ece": 0.0, "missing": 0, "missing_rate": 0.0}
    judged = [(max(p, 1 - p), (p >= 0.5) == truth) for p, truth in pairs if p is not None]
    missing = len(pairs) - len(judged)
    return {"n": len(pairs), "accuracy": sum(1 for _, ok in judged if ok) / len(pairs),
            "ece": expected_calibration_error(judged), "missing": missing,
            "missing_rate": missing / len(pairs)}


def run_gate_shadow(client: JevClient, dataset: list[dict[str, Any]], anonymize: bool = True) -> StageResult:
    """Cada linha: e-mail, triagem, fatos, tool call proposta e rótulos appropriate/args_complete."""
    pairs: dict[str, list[tuple[float | None, bool]]] = {"appropriate": [], "args_complete": []}
    rows, latencies = [], []
    for row in dataset:
        state = decisions.gate_state(row["email"], row.get("triage", {}), row["tool"], row["args"],
                                     row.get("facts", []))
        if anonymize:
            state = Pseudonymizer().apply(state)
        response = client.ask(state, decisions.gate_questions(row["tool"]))
        latencies.append(response.latency_ms)
        out = {"id": row["id"], "tool": row["tool"]}
        for key in pairs:
            p = valid_noul(response, key)
            pairs[key].append((p, bool(row["labels"][key])))
            out[key] = round(p, 2) if p is not None else None
            out[f"{key}_label"] = bool(row["labels"][key])
        rows.append(out)
    return StageResult("gate", len(dataset), {k: _noul_metrics(v) for k, v in pairs.items()}, rows,
                       statistics.fmean(latencies) if latencies else 0.0)


def run_verify_shadow(client: JevClient, dataset: list[dict[str, Any]], anonymize: bool = True,
                      tenant: Tenant | None = None) -> StageResult:
    """Cada linha: e-mail, fatos, rascunho e rótulos resolves/unsupported_claims/quality (1-5)."""
    pairs: dict[str, list[tuple[float | None, bool]]] = {"resolves": [], "unsupported_claims": []}
    quality_ok, quality_missing, rows, latencies = 0, 0, [], []
    for row in dataset:
        state = decisions.verify_state(row["email"], row["draft"], row.get("facts", []))
        if anonymize:
            state = Pseudonymizer().apply(state)
        response = client.ask(state, decisions.verify_questions(tenant))
        latencies.append(response.latency_ms)
        out = {"id": row["id"]}
        for key in pairs:
            p = valid_noul(response, key)
            pairs[key].append((p, bool(row["labels"][key])))
            out[key] = round(p, 2) if p is not None else None
            out[f"{key}_label"] = bool(row["labels"][key])
        quality = _score(response, "quality")
        quality_missing += quality is None
        within = quality is not None and abs(round(quality) - int(row["labels"]["quality"])) <= 1
        quality_ok += within
        out["quality"] = round(quality, 1) if quality is not None else None
        out["quality_label"] = int(row["labels"]["quality"])
        rows.append(out)
    metrics = {k: _noul_metrics(v) for k, v in pairs.items()}
    metrics["quality"] = {"n": len(dataset), "within_one": quality_ok / len(dataset) if dataset else 0.0,
                          "missing": quality_missing,
                          "missing_rate": quality_missing / len(dataset) if dataset else 0.0}
    return StageResult("verify", len(dataset), metrics, rows,
                       statistics.fmean(latencies) if latencies else 0.0)


def suggest_thresholds(rows: list[dict[str, Any]], target_precision: float = 0.95,
                       min_support: int = 3) -> dict[str, float | None]:
    """Para cada categoria prevista, o menor limiar de confiança com precisão >= alvo.

    None significa que nenhum limiar atinge o alvo com suporte mínimo: essa categoria deve
    ficar em revisão humana até haver mais dados.
    """
    by_category: dict[str, list[tuple[float, bool]]] = {}
    for row in rows:
        if row["predicted"] is None:  # sem resposta: não sustenta limiar nenhum
            continue
        by_category.setdefault(row["predicted"], []).append(
            (float(row["confidence"]), row["predicted"] == row["expected"]))
    suggestions: dict[str, float | None] = {}
    for category, pairs in sorted(by_category.items()):
        chosen: float | None = None
        for threshold in sorted({c for c, _ in pairs}):
            kept = [ok for c, ok in pairs if c >= threshold]
            if len(kept) >= min_support and sum(kept) / len(kept) >= target_precision:
                chosen = threshold
                break
        suggestions[category] = chosen
    return suggestions
