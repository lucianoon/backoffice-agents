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
from .llm import build_llm
from .storage import Store

TERMINAL = {"sent", "escalated", "discarded"}


@dataclass
class Runner:
    settings: Settings
    llm: BaseChatModel
    jev: JevClient
    adapters: Adapters
    store: Store

    def __post_init__(self) -> None:
        self.graph = build_graph(Nodes(self.settings, self.llm, self.jev, self.adapters, self.store))

    @classmethod
    def from_settings(cls, settings: Settings, llm: BaseChatModel | None = None,
                      adapters: Adapters | None = None) -> "Runner":
        llm = llm or build_llm(settings)
        return cls(settings=settings, llm=llm, jev=build_jev_client(settings, llm),
                   adapters=adapters or build_adapters(settings), store=Store(settings.db_path))

    # ---- ingestão ----
    def ingest_emails(self) -> list[str]:
        ids = []
        for message in self.adapters.email.fetch_unread():
            item_id = f"email:{message.id}"
            if self.store.get_item(item_id) is None:
                self.store.upsert_item(item_id, "email", "new", message.model_dump())
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
                                 "pending_action": None, "approval": None, "forwarded": []}
        self.store.set_item_state(item_id, "processing", state)
        return self._invoke(item_id, state)

    def resume_item(self, approval: dict[str, Any]) -> dict[str, Any]:
        item = self.store.get_item(approval["item_id"])
        if item is None or not item["state"]:
            raise KeyError(approval["item_id"])
        state = dict(item["state"])
        state["approval"] = {"id": approval["id"], "kind": approval["kind"],
                             "approved": approval["status"] == "approved"}
        result = self._invoke(approval["item_id"], state)
        self.store.mark_approval_applied(approval["id"])
        return result

    def run_pending(self) -> list[tuple[str, str]]:
        """Processa itens novos e retoma os que tiveram aprovação decidida. Devolve (item, status)."""
        outcomes: list[tuple[str, str]] = []
        for item in self.store.list_items("new"):
            outcomes.append((item["id"], self.process_item(item["id"])["status"]))
        for approval in self.store.list_approvals("approved") + self.store.list_approvals("rejected"):
            outcomes.append((approval["item_id"], self.resume_item(approval)["status"]))
        return outcomes

    def _invoke(self, item_id: str, state: dict[str, Any]) -> dict[str, Any]:
        try:
            final = self.graph.invoke(state)
        except Exception as exc:
            state["status"] = "error"
            state["notes"] = state.get("notes", []) + [f"erro: {exc.__class__.__name__}: {exc}"]
            self.store.set_item_state(item_id, "error", state)
            raise
        self.store.set_item_state(item_id, final.get("status", "unknown"), final)
        return final
