from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    item_id: str
    email: dict[str, Any]
    thread_id: str
    thread: list[dict[str, Any]]           # histórico da conversa (itens anteriores)
    attachments: list[dict[str, Any]]      # texto extraído dos anexos
    triage: dict[str, Any]
    tier: str                          # auto | review | escalate
    messages: list[dict[str, Any]]     # mensagens LangChain serializadas
    draft_reply: str
    verification: dict[str, Any]
    pending_action: dict[str, Any] | None   # tool call aguardando aprovação
    approval: dict[str, Any] | None         # decisão humana injetada na retomada
    status: str
    sent_at: str | None                # marcador de envio concluído (idempotência)
    sent_message_id: str               # Message-ID da resposta enviada (liga respostas do cliente à thread)
    regenerations: int
    notes: list[str]
    escalation_reason: str | None
    forwarded: list[dict[str, str]]
