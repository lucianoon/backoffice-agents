from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    item_id: str
    email: dict[str, Any]
    triage: dict[str, Any]
    tier: str                          # auto | review | escalate
    messages: list[dict[str, Any]]     # mensagens LangChain serializadas
    draft_reply: str
    verification: dict[str, Any]
    pending_action: dict[str, Any] | None   # tool call aguardando aprovação
    approval: dict[str, Any] | None         # decisão humana injetada na retomada
    status: str
    sent_at: str | None                # marcador de envio concluído (idempotência)
    regenerations: int
    notes: list[str]
    escalation_reason: str | None
    forwarded: list[dict[str, str]]
