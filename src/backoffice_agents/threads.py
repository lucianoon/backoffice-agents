"""Contexto de conversa: liga um e-mail novo à thread certa e monta o histórico para o agente."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .adapters.email import EmailMessage
from .storage import Store, normalize_subject

EXCERPT = 600
OPEN_STATUSES = {"new", "processing", "awaiting_approval", "error", "sending"}


def resolve_thread(store: Store, message: EmailMessage, item_id: str, window_days: int) -> str:
    """Thread por cabeçalhos (In-Reply-To/References, inclusive das nossas respostas); senão, por
    remetente + assunto normalizado dentro da janela; senão, o item abre uma thread nova."""
    for ref in [message.in_reply_to, *reversed(message.references)]:
        if ref:
            item = store.find_item_by_message_id(ref)
            if item:
                return item.get("thread_id") or item["id"]
    since = (datetime.now(UTC) - timedelta(days=window_days)).isoformat(timespec="seconds")
    item = store.find_thread_by_subject(message.from_addr, normalize_subject(message.subject), since)
    if item:
        return item.get("thread_id") or item["id"]
    return item_id


def thread_history(store: Store, thread_id: str | None, exclude_item: str) -> list[dict[str, Any]]:
    """Itens anteriores da thread, do mais antigo ao mais novo, com a resposta enviada quando houve."""
    if not thread_id:
        return []
    history = []
    for item in store.list_thread(thread_id):
        if item["id"] == exclude_item:
            continue
        payload, state = item["payload"], item.get("state") or {}
        entry = {
            "item_id": item["id"],
            "date": payload.get("date", ""),
            "from": payload.get("from_addr", ""),
            "subject": payload.get("subject", ""),
            "customer_message": _clip(payload.get("body", "")),
            "status": item["status"],
            "open": item["status"] in OPEN_STATUSES,
            "category": state.get("triage", {}).get("category"),
        }
        if state.get("sent_at") and state.get("draft_reply"):
            entry["our_reply"] = _clip(state["draft_reply"])
        elif item["status"] == "escalated":
            entry["our_reply"] = "[escalado para um humano; resposta fora do sistema]"
        history.append(entry)
    return history


def format_history(history: list[dict[str, Any]]) -> str:
    if not history:
        return ""
    lines = ["HISTÓRICO DA CONVERSA (do mais antigo ao mais novo):"]
    for i, h in enumerate(history, 1):
        status = "EM ABERTO" if h["open"] else h["status"]
        lines.append(f"\n[{i}] {h['date']} — cliente ({status}):\n{h['customer_message']}")
        if h.get("our_reply"):
            lines.append(f"\n[{i}] nossa resposta:\n{h['our_reply']}")
    return "\n".join(lines)


def _clip(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= EXCERPT else text[:EXCERPT] + " [...]"
