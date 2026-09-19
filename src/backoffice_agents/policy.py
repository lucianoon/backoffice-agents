"""Política de decisão: faixas de confiança e nível de risco das ferramentas.

Regra central: probabilidade nunca autoriza ação irreversível sozinha.
CRITICAL e HIGH sempre passam por aprovação humana; MEDIUM passa pelo gate do
Jev; LOW executa direto.
"""

from __future__ import annotations

from enum import StrEnum

from .config import Settings


class Tier(StrEnum):
    AUTO = "auto"          # confiança alta: segue sem humano
    REVIEW = "review"      # confiança média: humano aprova a saída
    ESCALATE = "escalate"  # confiança baixa ou pedido explícito: humano assume


class RiskLevel(StrEnum):
    LOW = "low"            # leitura
    MEDIUM = "medium"      # escrita reversível (registrar interação, responder e-mail)
    HIGH = "high"          # escrita com efeito financeiro (criar pedido, abrir oportunidade)
    CRITICAL = "critical"  # irreversível (cancelar pedido, estornar)


class GateOutcome(StrEnum):
    EXECUTE = "execute"
    APPROVE = "approve"    # pede aprovação humana antes de executar
    REJECT = "reject"      # devolve ao LLM com o motivo


def tier_for(confidence: float, settings: Settings, category: str | None = None) -> Tier:
    """Faixa de confiança; limiares por categoria (quando definidos) vencem os globais."""
    auto = settings.confidence_auto_by_category.get(category or "", settings.confidence_auto)
    review = settings.confidence_review_by_category.get(category or "", settings.confidence_review)
    if confidence >= auto:
        return Tier.AUTO
    if confidence >= review:
        return Tier.REVIEW
    return Tier.ESCALATE


def gate_outcome(risk: RiskLevel, appropriate: float | None, args_complete: float | None,
                 settings: Settings) -> tuple[GateOutcome, str]:
    """Decide o que fazer com uma chamada de ferramenta proposta pelo LLM.

    `appropriate` e `args_complete` são os nouls do Jev (None quando o gate não foi consultado).
    """
    if risk == RiskLevel.CRITICAL:
        return GateOutcome.APPROVE, "ação irreversível: aprovação humana obrigatória"
    if risk == RiskLevel.HIGH:
        return GateOutcome.APPROVE, "ação de alto risco: aprovação humana obrigatória"
    if risk == RiskLevel.LOW:
        return GateOutcome.EXECUTE, "leitura: executa direto"

    # MEDIUM: depende do gate
    if appropriate is None or args_complete is None:
        return GateOutcome.APPROVE, "gate indisponível: cai para aprovação humana"
    if appropriate < 0.5:
        return GateOutcome.REJECT, f"ferramenta não parece adequada à tarefa (p={appropriate:.2f})"
    if args_complete < 0.5:
        return GateOutcome.REJECT, f"argumentos incompletos ou inconsistentes (p={args_complete:.2f})"
    if min(appropriate, args_complete) < settings.gate_min_confidence:
        return GateOutcome.APPROVE, "gate com confiança insuficiente: aprovação humana"
    return GateOutcome.EXECUTE, "gate aprovou"
