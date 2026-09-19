"""Custo e latência reais por modelo, a partir da tabela `model_calls`.

Inclui o "e se": quanto as decisões hoje tomadas pelo emulador custariam no Jev real
(mesmos tokens de entrada, saída grátis), que é a economia que o piloto precisa provar.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .config import Settings


@dataclass
class ModelUsage:
    kind: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    mean_latency_ms: float
    p95_latency_ms: float


@dataclass
class CostReport:
    by_model: list[ModelUsage]
    items: int
    total_cost_usd: float
    llm_cost_usd: float
    jev_cost_usd: float
    emulated_input_tokens: int
    emulated_cost_usd: float          # o que o emulador custou (preço de LLM)
    what_if_real_jev_usd: float       # o que o Jev real cobraria pelos mesmos tokens de entrada

    @property
    def cost_per_item_usd(self) -> float:
        return self.total_cost_usd / self.items if self.items else 0.0


def _p95(values: list[float]) -> float:
    if len(values) < 2:
        return values[0] if values else 0.0
    return statistics.quantiles(values, n=20)[-1]


def _cost(kind: str, calibrated: bool, input_tokens: int, output_tokens: int, s: Settings) -> float:
    if kind == "jev" and calibrated:
        return input_tokens * s.jev_price_input_per_m / 1e6
    # LLM do agente e emulador (que é o mesmo LLM) pagam preço de LLM
    return (input_tokens * s.llm_price_input_per_m + output_tokens * s.llm_price_output_per_m) / 1e6


def cost_report(calls: list[dict[str, Any]], settings: Settings) -> CostReport:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for call in calls:
        groups[(call["kind"], call["model"])].append(call)

    by_model: list[ModelUsage] = []
    llm_cost = jev_cost = emulated_cost = what_if = 0.0
    emulated_tokens = 0
    for (kind, model), rows in sorted(groups.items()):
        cost = sum(_cost(kind, bool(r["calibrated"]), r["input_tokens"], r["output_tokens"], settings)
                   for r in rows)
        latencies = [float(r["latency_ms"]) for r in rows]
        by_model.append(ModelUsage(
            kind=kind, model=model, calls=len(rows),
            input_tokens=sum(int(r["input_tokens"]) for r in rows),
            output_tokens=sum(int(r["output_tokens"]) for r in rows),
            cost_usd=cost, mean_latency_ms=statistics.fmean(latencies), p95_latency_ms=_p95(latencies)))
        if kind == "llm":
            llm_cost += cost
        else:
            jev_cost += cost
            emulated_rows = [r for r in rows if not r["calibrated"]]
            emulated_cost += sum(_cost("jev", False, r["input_tokens"], r["output_tokens"], settings)
                                 for r in emulated_rows)
            emulated_tokens += sum(int(r["input_tokens"]) for r in emulated_rows)
    what_if = emulated_tokens * settings.jev_price_input_per_m / 1e6
    return CostReport(
        by_model=by_model, items=len({c["item_id"] for c in calls}),
        total_cost_usd=llm_cost + jev_cost, llm_cost_usd=llm_cost, jev_cost_usd=jev_cost,
        emulated_input_tokens=emulated_tokens, emulated_cost_usd=emulated_cost,
        what_if_real_jev_usd=what_if)
