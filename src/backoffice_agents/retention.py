"""Retenção de dados (LGPD): expurgo em duas fases dos itens encerrados.

1. Após `RETENTION_REDACT_DAYS`: o conteúdo do cliente (corpo, assunto, remetente, anexos,
   mensagens do agente, rascunho, fatos) é removido do item; ficam só os metadados que a
   calibração e o custo usam (status, triagem, tier, números da verificação, notas).
2. Após `RETENTION_DELETE_DAYS`: o item e tudo o que aponta para ele (decisões, chamadas de
   modelo, aprovações) são apagados.

Itens em aberto nunca são tocados. O worker roda o expurgo a cada ciclo; `backoffice purge`
roda sob demanda, com `--dry-run` para só listar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .config import Settings
from .obs import log_event
from .storage import Store

TERMINAL = ("sent", "escalated", "discarded", "failed", "skipped")
REDACTED = "[expurgado por retenção]"


@dataclass
class PurgeResult:
    redacted: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)


def redacted_payload(payload: dict) -> dict:
    return {"from_addr": REDACTED, "from_name": "", "to": payload.get("to", ""), "subject": REDACTED,
            "body": REDACTED, "date": payload.get("date", ""), "message_id": "", "in_reply_to": "",
            "references": [], "attachments": []}


def redacted_state(state: dict | None) -> dict:
    state = state or {}
    verification = dict(state.get("verification") or {})
    verification.pop("claims", None)                      # os fatos citam dados do cliente
    return {"status": state.get("status"), "tier": state.get("tier"), "triage": state.get("triage"),
            "verification": verification, "notes": state.get("notes", []), "sent_at": state.get("sent_at"),
            "escalation_reason": state.get("escalation_reason"), "regenerations": state.get("regenerations"),
            "redacted": True}


def purge(store: Store, settings: Settings, now: datetime | None = None,
          dry_run: bool = False) -> PurgeResult:
    now = now or datetime.now(UTC)
    result = PurgeResult()
    delete_before = (now - timedelta(days=settings.retention_delete_days)).isoformat(timespec="seconds")
    redact_before = (now - timedelta(days=settings.retention_redact_days)).isoformat(timespec="seconds")

    to_delete = store.list_terminal_before(delete_before, TERMINAL)
    result.deleted = [i["id"] for i in to_delete]
    if result.deleted and not dry_run:
        store.delete_items(result.deleted)

    to_redact = [i for i in store.list_terminal_before(redact_before, TERMINAL)
                 if i["id"] not in result.deleted and not i.get("redacted_at")]
    result.redacted = [i["id"] for i in to_redact]
    if not dry_run:
        for item in to_redact:
            store.redact_item(item["id"], redacted_payload(item["payload"]), redacted_state(item["state"]))
    if (result.redacted or result.deleted) and not dry_run:
        log_event("purge", redacted=len(result.redacted), deleted=len(result.deleted))
    return result
