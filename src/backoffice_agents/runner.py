"""Monta as dependências e executa/retoma itens de trabalho."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.language_models import BaseChatModel

from .adapters import Adapters, build_adapters
from .config import Settings
from .graph import build_graph
from .graph.nodes import Nodes
from .jev import JevClient, build_jev_client
from .jev.emulated import EmulatedJevClient
from .llm import build_emulator_llm, build_llm
from .metrics import alerts, collect_metrics
from .obs import log_event, setup_logging
from .retention import PurgeResult, purge
from .storage import Store
from .tenant import Tenant, load_tenant
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
    tenant: Tenant | None = None

    def __post_init__(self) -> None:
        setup_logging(self.settings.log_format, self.settings.log_level)
        configure_tracing(self.settings)
        self._last_alerts: dict[str, datetime] = {}
        self.tenant = self.tenant or load_tenant(self.settings.tenant_file)
        self.graph = build_graph(Nodes(self.settings, self.llm, self.jev, self.adapters, self.store,
                                       jev_fallback=self.jev_fallback, tenant=self.tenant))

    @classmethod
    def from_settings(cls, settings: Settings, llm: BaseChatModel | None = None,
                      adapters: Adapters | None = None) -> Runner:
        llm = llm or build_llm(settings)
        emulator_llm = build_emulator_llm(settings, llm)   # EMULATOR_MODEL: mais barato so para decidir
        fallback = None
        if settings.jev_mode == "real" and settings.jev_fallback_emulated:
            fallback = EmulatedJevClient(emulator_llm)
        return cls(settings=settings, llm=llm, jev=build_jev_client(settings, emulator_llm),
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

        def claim_items(limit: int) -> list[dict[str, Any]]:
            batch = []
            while len(batch) < limit and (item := self.store.claim_next(
                    self.settings.max_attempts, self.settings.retry_delay_s, exclude=seen)) is not None:
                seen.add(item["id"])
                batch.append(item)
            return batch

        def run_item(item: dict[str, Any]) -> tuple[str, str]:
            try:
                return item["id"], self.process_item(item["id"])["status"]
            except Exception:
                return item["id"], self.store.get_item(item["id"])["status"]

        def run_approval(approval: dict[str, Any]) -> tuple[str, str]:
            try:
                return approval["item_id"], self.resume_item(approval)["status"]
            except Exception:
                return approval["item_id"], self.store.get_item(approval["item_id"])["status"]

        workers = max(1, self.settings.worker_concurrency)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while batch := claim_items(workers):
                outcomes += list(pool.map(run_item, batch))
            approvals_batch: list[dict[str, Any]] = []
            while (approval := self.store.claim_next_approval()) is not None:
                approvals_batch.append(approval)
            if approvals_batch:
                outcomes += list(pool.map(run_approval, approvals_batch))
        self.check_alerts()
        return outcomes

    def purge(self, dry_run: bool = False) -> PurgeResult:
        """Expurgo por retencao (LGPD): redige e apaga itens encerrados conforme os prazos."""
        return purge(self.store, self.settings, dry_run=dry_run)

    def check_alerts(self) -> list[str]:
        """Avisa o operador no Telegram sobre fila cheia, aprovacoes envelhecendo e taxa de erro."""
        metrics = collect_metrics(self.store, self.settings)
        now = datetime.now(UTC)
        cooldown = timedelta(minutes=self.settings.alert_cooldown_min)
        fired = []
        for text in alerts(metrics, self.settings):
            key = text.split(" (")[0]
            last = self._last_alerts.get(key)
            if last and now - last < cooldown:
                continue
            self._last_alerts[key] = now
            self.adapters.telegram.send_message(f"Alerta: {text}")
            log_event("alert", level=30, alert=text)
            fired.append(text)
        return fired

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
            log_event("item_error", level=40, item_id=item_id, status=status, attempts=attempts,
                      error=f"{exc.__class__.__name__}: {exc}")
            if status == "failed":
                self.adapters.telegram.send_message(
                    f"❌ Falhou após {attempts} tentativas — item {item_id}\n"
                    f"{state['email'].get('subject')}\n{exc.__class__.__name__}: {exc}")
            raise
        self.store.set_item_state(item_id, final.get("status", "unknown"), final)
        log_event("item_done", item_id=item_id, status=final.get("status"), tier=final.get("tier"),
                  resume=bool(state.get("approval")))
        return final
