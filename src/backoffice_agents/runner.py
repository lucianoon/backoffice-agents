"""Monta as dependências e executa/retoma itens de trabalho."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel

from .adapters import Adapters, build_adapters
from .config import Settings
from .graph import build_graph
from .graph.nodes import Nodes
from .jev import JevClient, build_jev_client
from .jev.emulated import EmulatedJevClient
from .llm import build_llm
from .storage import Store
from .threads import resolve_thread
from .tracing import configure_tracing, run_config

TERMINAL = {"sent", "escalated", "discarded", "failed"}


@dataclass
class Runner:
    settings: Settings
    llm: BaseChatModel
    jev: JevClient
    adapters: Adapters
    store: Store
    jev_fallback: JevClient | None = None

    def __post_init__(self) -> None:
        configure_tracing(self.settings)
        self.graph = build_graph(Nodes(self.settings, self.llm, self.jev, self.adapters, self.store,
                                       jev_fallback=self.jev_fallback))

    @classmethod
    def from_settings(cls, settings: Settings, llm: BaseChatModel | None = None,
                      adapters: Adapters | None = None) -> Runner:
        llm = llm or build_llm(settings)
        fallback = None
        if settings.jev_mode == "real" and settings.jev_fallback_emulated:
            fallback = EmulatedJevClient(llm)
        return cls(settings=settings, llm=llm, jev=build_jev_client(settings, llm),
                   adapters=adapters or build_adapters(settings), store=Store(settings.db_url),
                   jev_fallback=fallback)

    # ---- ingestão ----
    def ingest_emails(self) -> list[str]:
        ids = []
        for message in self.adapters.email.fetch_unread():
            item_id = f"email:{message.id}"
            if self.store.get_item(item_id) is None:
                thread_id = resolve_thread(self.store, message, item_id, self.settings.thread_window_days)
                self.store.upsert_item(item_id, "email", "new", message.model_dump(),
                                       thread_id=thread_id, message_id=message.message_id)
                ids.append(item_id)
            self.adapters.email.mark_processed(message.id)
        return ids

    # ---- execução ----
    def process_item(self, item_id: str) -> dict[str, Any]:
        item = self.store.get_item(item_id)
        if item is None:
            raise KeyError(item_id)
        state: dict[str, Any] = {"item_id": item_id, "email": item["payload"], "messages": [],
                                 "regenerations": 0, "notes": [], "status": "processing",
                                 "pending_action": None, "approval": None, "forwarded": [],
                                 "sent_at": None, "thread_id": item.get("thread_id") or item_id}
        self.store.set_item_state(item_id, "processing", state)
        return self._invoke(item_id, state)

    def resume_item(self, approval: dict[str, Any]) -> dict[str, Any]:
        """Retoma um item a partir de uma aprovação decidida.

        Aceita a aprovação em `approved`/`rejected` (marca `applying` aqui) ou já reservada por
        `claim_next_approval` (vem com `decided_status`).
        """
        item = self.store.get_item(approval["item_id"])
        if item is None or not item["state"]:
            raise KeyError(approval["item_id"])
        decided = approval.get("decided_status") or approval["status"]
        state = dict(item["state"])
        state["approval"] = {"id": approval["id"], "kind": approval["kind"],
                             "approved": decided == "approved"}
        if approval["status"] != "applying":
            # marcador em disco: se morrer no meio, a retomada vira escalada, não repetição
            self.store.set_approval_status(approval["id"], "applying")
        result = self._invoke(approval["item_id"], state)
        self.store.mark_approval_applied(approval["id"])
        return result

    def run_pending(self) -> list[tuple[str, str]]:
        """Recupera itens em voo, consome a fila (novos e em erro) e retoma aprovações.

        Cada item é reservado atomicamente, então vários workers podem chamar isto em paralelo.
        """
        outcomes: list[tuple[str, str]] = []
        outcomes += self._recover_in_flight()
        seen: set[str] = set()
        while (item := self.store.claim_next(self.settings.max_attempts, self.settings.retry_delay_s,
                                             exclude=seen)) is not None:
            seen.add(item["id"])
            try:
                outcomes.append((item["id"], self.process_item(item["id"])["status"]))
            except Exception:
                outcomes.append((item["id"], self.store.get_item(item["id"])["status"]))
        while (approval := self.store.claim_next_approval()) is not None:
            try:
                outcomes.append((approval["item_id"], self.resume_item(approval)["status"]))
            except Exception:
                outcomes.append((approval["item_id"], self.store.get_item(approval["item_id"])["status"]))
        return outcomes

    def _recover_in_flight(self) -> list[tuple[str, str]]:
        """Itens que morreram no meio de um efeito externo não são repetidos: um humano confirma."""
        outcomes = []
        for item in self.store.list_items("sending"):
            outcomes.append((item["id"], self._escalate_ambiguous(
                item, "processo interrompido durante o envio do e-mail; confirmar se a resposta saiu")))
        for approval in self.store.list_approvals("applying"):
            item = self.store.get_item(approval["item_id"])
            self.store.set_approval_status(approval["id"], "applied")
            if item and item["status"] not in TERMINAL:
                outcomes.append((item["id"], self._escalate_ambiguous(
                    item, f"processo interrompido ao aplicar a aprovação #{approval['id']} "
                          f"({approval['action'].get('tool') or 'envio'}); confirmar no sistema de destino")))
        return outcomes

    def _escalate_ambiguous(self, item: dict[str, Any], reason: str) -> str:
        state = dict(item["state"] or {})
        state.update({"status": "escalated", "escalation_reason": reason, "approval": None,
                      "notes": state.get("notes", []) + [f"recuperação: {reason}"]})
        self.store.set_item_state(item["id"], "escalated", state)
        self.adapters.telegram.send_message(
            f"⚠️ Recuperação — item {item['id']}\n{item['payload'].get('subject')}\n{reason}")
        return "escalated"

    def _invoke(self, item_id: str, state: dict[str, Any]) -> dict[str, Any]:
        config = run_config(self.settings, item_id, {"resume": bool(state.get("approval"))})
        try:
            final = self.graph.invoke(state, config=config)
        except Exception as exc:
            attempts = self.store.increment_attempts(item_id)
            status = "failed" if attempts >= self.settings.max_attempts else "error"
            state["status"] = status
            state["notes"] = state.get("notes", []) + [
                f"erro (tentativa {attempts}/{self.settings.max_attempts}): {exc.__class__.__name__}: {exc}"]
            self.store.set_item_state(item_id, status, state)
            if status == "failed":
                self.adapters.telegram.send_message(
                    f"❌ Falhou após {attempts} tentativas — item {item_id}\n"
                    f"{state['email'].get('subject')}\n{exc.__class__.__name__}: {exc}")
            raise
        self.store.set_item_state(item_id, final.get("status", "unknown"), final)
        return final
