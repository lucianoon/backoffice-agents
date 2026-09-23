"""Nós do grafo. Cada nó recebe o estado serializável e devolve só o que mudou."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
    messages_to_dict,
)

from .. import decisions
from ..adapters import Adapters
from ..adapters.email import Attachment, EmailMessage
from ..adapters.telegram import Button, MockTelegramAdapter
from ..agent_loop import execute_tool, run_agent
from ..attachments import extract_all, needs_vision
from ..budget import fit_state
from ..claims import split_reply
from ..config import Settings
from ..jev import ChoiceAnswer, JevClient, JevResponse, NoulAnswer, ScoreAnswer
from ..knowledge import KnowledgeBase
from ..obs import log_event
from ..policy import GateOutcome, RiskLevel, Tier, gate_outcome, tier_for
from ..privacy import Pseudonymizer
from ..storage import Store
from ..tenant import Tenant, default_tenant
from ..threads import format_history, thread_history
from ..tools import ToolContext, ToolRegistry, build_tools, parse_allowlist
from ..tracing import traced_jev_ask
from .state import AgentState

# O prompt do sistema vem do tenant (tenants/*.toml); ver tenant.DEFAULT_SYSTEM_PROMPT.


def _probability(value: Any) -> float | None:
    """Número finito em [0, 1]; qualquer outra coisa (None, str, bool, NaN) é None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    value = float(value)
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else None


def valid_noul(response: JevResponse, key: str) -> float | None:
    """Noul `key` da resposta, ou None se ausente, de outro tipo ou não numérico."""
    answer = response.answers.get(key)
    return _probability(answer.noul) if isinstance(answer, NoulAnswer) else None


def invalid_triage_keys(response: JevResponse) -> list[str]:
    """Perguntas da triagem (além de injection) sem resposta utilizável."""
    invalid = [key for key in ("needs_human", "sensitive") if valid_noul(response, key) is None]
    category = response.answers.get("category")
    if not isinstance(category, ChoiceAnswer) or _probability(category.confidence) is None:
        invalid.append("category")
    urgency = response.answers.get("urgency")
    if not isinstance(urgency, ScoreAnswer) or not _finite(urgency.score):
        invalid.append("urgency")
    return invalid


def _finite(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value)


class Nodes:
    def __init__(self, settings: Settings, llm: BaseChatModel, jev: JevClient, adapters: Adapters,
                 store: Store, jev_fallback: JevClient | None = None,
                 tenant: Tenant | None = None) -> None:
        self.settings = settings
        self.tenant = tenant or default_tenant()
        self.llm = llm
        self.jev = jev
        self.jev_fallback = jev_fallback
        self.adapters = adapters
        self.store = store
        kb_dir = Path(settings.kb_dir)
        self.kb = (KnowledgeBase(kb_dir, jev, settings.kb_candidates, settings.kb_top_k,
                                 settings.kb_min_score, anonymize=settings.jev_anonymize)
                   if kb_dir.is_dir() and any(kb_dir.glob("*.md")) else None)

    # ---------- helpers ----------
    def _log(self, item_id: str, stage: str, response: JevResponse) -> None:
        for key, answer in response.answers.items():
            try:  # resposta malformada é registrada com confiança zero, sem derrubar o item
                confidence = float(answer.confidence)
            except (TypeError, ValueError):
                confidence = 0.0
            if not math.isfinite(confidence):
                confidence = 0.0
            self.store.log_decision(item_id, stage, key, answer.type, answer.model_dump(warnings=False),
                                    confidence, response.calibrated, response.model, response.latency_ms,
                                    version=self.tenant.label)

    def _record_jev(self, item_id: str, stage: str, response: JevResponse) -> None:
        """Decisões + chamada de modelo: usado pelo _ask e pelas tools que consultam o Jev."""
        self._log(item_id, stage, response)
        self.store.log_model_call(item_id, "jev", stage, response.model,
                                  response.usage.get("input_tokens", 0),
                                  response.usage.get("output_tokens", 0),
                                  response.latency_ms, response.calibrated, version=self.tenant.label)

    def _ask(self, state: AgentState, stage: str, payload: dict[str, Any], questions,
             extra_names: Sequence[str] = ()) -> tuple[JevResponse, str | None]:
        """Pseudonimiza, consulta o Jev (com fallback) e registra. Devolve (resposta, nota)."""
        if self.settings.jev_anonymize:
            names = [state["email"].get("from_name", ""), *extra_names]
            payload = Pseudonymizer(names).apply(payload)
        payload, cuts = fit_state(payload, self.settings.jev_state_budget_tokens)
        notes: list[str] = []
        if cuts:
            notes.append(f"{stage}: estado reduzido para caber no Jev ({'; '.join(cuts)})")
        try:
            response = traced_jev_ask(self.jev.ask, self.settings.jev_model, payload, questions)
        except Exception as exc:
            if self.jev_fallback is None:
                raise
            response = traced_jev_ask(self.jev_fallback.ask, "jev-emulated", payload, questions)
            notes.append(f"{stage}: Jev indisponível ({exc.__class__.__name__}); usado fallback emulado")
        self._record_jev(state["item_id"], stage, response)
        return response, (" | ".join(notes) or None)

    def _on_llm_call(self, item_id: str, stage: str):
        def hook(message, latency_ms: float) -> None:
            usage = getattr(message, "usage_metadata", None) or {}
            self.store.log_model_call(item_id, "llm", stage, self.settings.llm_model,
                                      usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                                      latency_ms, version=self.tenant.label)
        return hook

    @staticmethod
    def _label_buttons(item_id: str) -> list[Button]:
        """Botões de rótulo humano da categoria: alimentam a medição de calibração."""
        return [Button(text="👍 Categoria ok", callback_data=f"lbl:ok:{item_id}"),
                Button(text="✏️ Corrigir categoria", callback_data=f"lbl:fix:{item_id}")]

    def _registry(self, state: AgentState) -> ToolRegistry:
        item_id = state["item_id"]
        context = ToolContext(customer_email=state["email"]["from_addr"],
                              escalation_reason=state.get("escalation_reason"),
                              forwarded=list(state.get("forwarded", [])),
                              kb=self.kb,
                              log_jev=lambda stage, response: self._record_jev(item_id, stage, response),
                              forward_allowlist=parse_allowlist(self.settings.email_forward_allowlist))
        return build_tools(self.adapters, context)

    @staticmethod
    def _facts(messages: list[BaseMessage]) -> list[dict[str, Any]]:
        facts = []
        for m in messages:
            if isinstance(m, ToolMessage):
                result: Any = m.content
                if isinstance(m.content, str):
                    try:
                        result = json.loads(m.content)
                    except json.JSONDecodeError:
                        pass
                facts.append({"tool": m.name, "result": result})
        return facts

    def _initial_messages(self, state: AgentState, triage: dict[str, Any],
                          history: list[dict[str, Any]],
                          attachments: list[dict[str, Any]]) -> list[BaseMessage]:
        email = state["email"]
        parts = [f"TRIAGEM: categoria={triage.get('category')} urgência={triage.get('urgency')}/5"]
        if history:
            parts.append(format_history(history))
        parts.append(
            f"E-MAIL RECEBIDO\nDe: {email.get('from_name')} <{email.get('from_addr')}>\n"
            f"Assunto: {email.get('subject')}\nData: {email.get('date')}\n\n{email.get('body')}")
        if attachments:
            parts.append("ANEXOS (texto extraído):\n" + "\n\n".join(
                f"--- {a['filename']} ({a['content_type']}) ---\n{a['text']}" for a in attachments))
        return [SystemMessage(content=self.tenant.rendered_prompt()),
                HumanMessage(content="\n\n".join(parts))]

    def _gate(self, state: AgentState, registry: ToolRegistry, messages: list[BaseMessage]):
        # Consultas idênticas dentro do mesmo passo de ação (o LLM repropõe a mesma chamada
        # com os mesmos dados) são respondidas do cache: economiza chamadas ao Jev no retry.
        cache: dict[str, tuple[GateOutcome, str]] = {}

        def gate(call: dict[str, Any]) -> tuple[GateOutcome, str]:
            risk = registry.risk.get(call["name"], RiskLevel.HIGH)
            appropriate = args_complete = None
            if risk == RiskLevel.MEDIUM:
                facts = self._facts(messages)
                cache_key = hashlib.sha256(json.dumps(
                    [call["name"], call["args"], facts], ensure_ascii=False, sort_keys=True,
                    default=str).encode("utf-8")).hexdigest()
                if cache_key in cache:
                    log_event("gate_cached", item_id=state["item_id"],
                                    tool=call["name"], cached=True)
                    return cache[cache_key]
                try:
                    response, _ = self._ask(
                        state, f"gate:{call['name']}",
                        decisions.gate_state(state["email"], state.get("triage", {}), call["name"],
                                             call["args"], facts),
                        decisions.gate_questions(call["name"]))
                    # ausente/malformado vira None: gate_outcome cai para aprovação humana
                    appropriate = valid_noul(response, "appropriate")
                    args_complete = valid_noul(response, "args_complete")
                except Exception as exc:  # Jev indisponível: fail-closed para aprovação humana
                    return GateOutcome.APPROVE, f"gate indisponível ({exc.__class__.__name__})"
                outcome = gate_outcome(risk, appropriate, args_complete, self.settings)
                cache[cache_key] = outcome
                return outcome
            return gate_outcome(risk, appropriate, args_complete, self.settings)
        return gate

    # ---------- triagem ----------
    def _triage_ask(self, state: AgentState, contact: Any, history: list[dict[str, Any]],
                    attachments: list[dict[str, Any]]) -> tuple[JevResponse, str | None]:
        return self._ask(
            state, "triage",
            decisions.triage_state(state["email"], contact.model_dump() if contact else None,
                                   history, attachments),
            decisions.triage_questions(self.tenant), extra_names=[contact.name] if contact else [])

    @staticmethod
    def _injection(response: JevResponse) -> float | None:
        """Probabilidade de prompt injection, ou None se o Jev não devolveu uma válida.

        Fail-closed: quem chama trata None como suspeito (o item escala sem chamar o LLM).
        """
        return valid_noul(response, "injection")

    def _injection_clear(self, response: JevResponse) -> bool:
        injection = self._injection(response)
        return injection is not None and injection < self.settings.injection_escalate

    def _triage_notes(self, response: JevResponse, fallback_note: str | None,
                      history: list[dict[str, Any]], attachments: list[dict[str, Any]]) -> list[str]:
        category = response.choice("category")
        notes = [(f"triagem: {category.choice} (conf {category.confidence:.2f}), "
                  f"urgência {response.score('urgency').score:.1f}, "
                  f"humano {response.noul('needs_human'):.2f}")]
        if fallback_note:
            notes.insert(0, fallback_note)
        if history:
            open_count = sum(1 for h in history if h["open"])
            notes.append(f"thread com {len(history)} mensagem(ns) anterior(es)"
                         + (f", {open_count} em aberto" if open_count else ""))
        if attachments:
            notes.append("anexos: " + ", ".join(f"{a['filename']} ({a['method']})" for a in attachments))
        return notes

    @staticmethod
    def _escalate(triage: dict[str, Any], notes: list[str], reason: str) -> dict[str, Any]:
        return {"triage": triage, "tier": Tier.ESCALATE, "status": "escalated", "notes": notes,
                "escalation_reason": reason}

    def triage(self, state: AgentState) -> dict[str, Any]:
        """Triagem em duas passadas para que conteúdo suspeito nunca chegue ao LLM do agente.

        1. Anexos extraídos sem LLM (PDF, texto; imagens ficam pendentes) e triagem pelo Jev.
           Se o gate de prompt injection disparar aqui, o item escala sem nenhuma chamada ao LLM,
           nem mesmo a transcrição de imagens.
        2. Só se o e-mail passou e há imagens, o LLM com visão as transcreve (chamada sem
           ferramentas) e a triagem é refeita com o texto transcrito: a transcrição passa pelo
           mesmo gate antes de entrar no prompt do agente.
        """
        email = state["email"]
        contact = self.adapters.crm.find_contact_by_email(email["from_addr"])
        history = thread_history(self.store, state.get("thread_id"), state["item_id"])
        raw = [Attachment(**a) for a in email.get("attachments", [])]
        attachments = extract_all(raw)  # sem LLM: nada do e-mail sai para o modelo antes do gate
        extra_notes: list[str] = []
        try:
            response, fallback_note = self._triage_ask(state, contact, history, attachments)
            if needs_vision(raw) and self._injection_clear(response):
                attachments = extract_all(raw, self.llm)
                response, fallback_note = self._triage_ask(state, contact, history, attachments)
                extra_notes.append("triagem refeita com a transcrição das imagens")
        except Exception as exc:  # sem Jev e sem fallback: humano assume, nada é perdido
            return {"thread": history, "attachments": attachments,
                    "tier": Tier.ESCALATE, "status": "escalated",
                    "escalation_reason": f"triagem indisponível ({exc.__class__.__name__}: {exc})",
                    "notes": [f"triagem falhou: {exc.__class__.__name__}"]}
        context_update = {"thread": history, "attachments": attachments}
        base_notes = ([fallback_note] if fallback_note else []) + extra_notes
        injection = self._injection(response)
        if injection is None:
            # resposta ausente, malformada ou não numérica: suspeita até prova em contrário
            log_event("triage_injection_invalid", item_id=state["item_id"])
            return context_update | self._escalate(
                {"injection": None, "calibrated": response.calibrated},
                base_notes + ["triagem: Jev sem probabilidade válida de prompt injection"],
                "triagem: gate de prompt injection sem resposta válida do Jev (fail-closed)")
        invalid = invalid_triage_keys(response)
        if invalid:
            log_event("triage_incomplete", item_id=state["item_id"], keys=invalid)
            return context_update | self._escalate(
                {"injection": round(injection, 3), "calibrated": response.calibrated},
                base_notes + [f"triagem: Jev sem resposta válida para {', '.join(invalid)}"],
                "triagem: resposta incompleta do Jev (fail-closed)")
        notes = self._triage_notes(response, fallback_note, history, attachments) + extra_notes
        return context_update | self._route_triage(state, response, notes, history, attachments,
                                                   injection)

    def _route_triage(self, state: AgentState, response: JevResponse, notes: list[str],
                      history: list[dict[str, Any]], attachments: list[dict[str, Any]],
                      injection: float) -> dict[str, Any]:
        category = response.choice("category")
        urgency = response.score("urgency")
        needs_human = response.noul("needs_human")
        sensitive = response.noul("sensitive")
        triage = {
            "category": category.choice,
            "category_confidence": category.confidence,
            "urgency": round(urgency.score, 2),
            "needs_human": round(needs_human, 3),
            "sensitive": round(sensitive, 3),
            "injection": round(injection, 3),
            "calibrated": response.calibrated,
        }
        if injection >= self.settings.injection_escalate:
            # nem o agente nem a transcrição de imagens chegam a ser chamados: humano decide
            return self._escalate(triage, notes, f"triagem: possível prompt injection (p={injection:.2f})")
        tier = tier_for(category.confidence, self.settings, category.choice)
        log_event("triage", item_id=state["item_id"], category=category.choice,
                  confidence=round(category.confidence, 2), tier=str(tier), urgency=round(urgency.score, 1),
                  needs_human=round(needs_human, 2), injection=round(injection, 2),
                  sensitive=round(sensitive, 2), calibrated=response.calibrated)
        if sensitive >= self.settings.sensitive_escalate:
            return self._escalate(triage, notes, f"triagem: dado sensível (p={sensitive:.2f})")
        if category.choice == "spam_irrelevante" and tier == Tier.AUTO:
            return {"triage": triage, "tier": Tier.ESCALATE, "status": "discarded", "notes": notes}
        if needs_human >= self.settings.needs_human_escalate:
            return self._escalate(triage, notes, "triagem: caso exige humano")
        if tier == Tier.ESCALATE:
            return self._escalate(triage, notes, "triagem: confiança baixa na categoria")
        return {"triage": triage, "tier": tier, "status": "triaged", "notes": notes,
                "messages": messages_to_dict(self._initial_messages(state, triage, history, attachments))}

    # ---------- nós ----------
    def act(self, state: AgentState) -> dict[str, Any]:
        registry = self._registry(state)
        messages = messages_from_dict(state["messages"])
        turn = run_agent(self.llm, registry, messages, self._gate(state, registry, messages),
                         self.settings.max_tool_iterations,
                         on_llm_call=self._on_llm_call(state["item_id"], "act"))
        update: dict[str, Any] = {
            "messages": messages_to_dict(turn.messages),
            "escalation_reason": registry.context.escalation_reason,
            "forwarded": registry.context.forwarded,
            "pending_action": None,
        }
        if turn.pending_call:
            update["pending_action"] = {"call": turn.pending_call, "reason": turn.pending_reason}
            update["status"] = "awaiting_approval"
        elif turn.escalated or registry.context.escalation_reason:
            update["status"] = "escalated"
            update["escalation_reason"] = registry.context.escalation_reason or "limite de iterações"
        else:
            reply, claims = split_reply(turn.final_text or "")
            update["draft_reply"] = reply
            update["claims"] = claims
            update["status"] = "drafted"
        log_event("act", item_id=state["item_id"], status=update["status"],
                  pending_tool=(turn.pending_call or {}).get("name"), claims=len(update.get("claims", [])))
        return update

    def resume_tool(self, state: AgentState) -> dict[str, Any]:
        """Aplica a decisão humana sobre a tool call pendente e devolve o controle ao agente."""
        approval = state["approval"] or {}
        call = (state.get("pending_action") or {}).get("call")
        messages = messages_from_dict(state["messages"])
        if call:
            if approval.get("approved"):
                registry = self._registry(state)
                messages.append(execute_tool(registry, call))
                note = f"operador aprovou {call['name']}"
            else:
                messages.append(ToolMessage(
                    content=json.dumps({"rejected": True, "reason": "operador humano rejeitou esta ação"},
                                       ensure_ascii=False),
                    tool_call_id=call["id"], name=call["name"]))
                note = f"operador rejeitou {call['name']}"
        else:
            note = "retomada sem ação pendente"
        return {"messages": messages_to_dict(messages), "pending_action": None, "approval": None,
                "notes": state.get("notes", []) + [note]}

    def verify(self, state: AgentState) -> dict[str, Any]:
        messages = messages_from_dict(state["messages"])
        draft = state.get("draft_reply", "")
        claims = list(state.get("claims") or [])
        try:
            response, fallback_note = self._ask(
                state, "verify",
                decisions.verify_state(state["email"], draft, self._facts(messages),
                                       state.get("thread"), state.get("attachments"), claims),
                decisions.verify_questions(self.tenant) | decisions.claim_questions(claims))
        except Exception as exc:  # sem verificação não se envia nada: humano revisa o rascunho
            return {"status": "escalated",
                    "escalation_reason": f"verificação indisponível ({exc.__class__.__name__})",
                    "notes": state.get("notes", []) + [f"verificação falhou: {exc.__class__.__name__}"]}
        resolves_p = valid_noul(response, "resolves")
        unsupported_p = valid_noul(response, "unsupported_claims")
        quality_answer = response.answers.get("quality")
        claim_ps = [valid_noul(response, f"claim_{i}") for i in range(len(claims))]
        invalid = ([k for k, v in (("resolves", resolves_p), ("unsupported_claims", unsupported_p))
                    if v is None]
                   + ([] if isinstance(quality_answer, ScoreAnswer) and _finite(quality_answer.score)
                      else ["quality"])
                   + [f"claim_{i}" for i, p in enumerate(claim_ps) if p is None])
        if invalid:  # sem veredito válido nada é enviado: humano revisa o rascunho
            log_event("verify_incomplete", item_id=state["item_id"], keys=invalid)
            return {"status": "escalated",
                    "escalation_reason": "verificação: resposta incompleta do Jev (fail-closed)",
                    "notes": state.get("notes", []) + [
                        f"verificação: Jev sem resposta válida para {', '.join(invalid)}"]}
        # só estreita os tipos: `invalid` vazio já garante estes valores
        assert resolves_p is not None and unsupported_p is not None
        assert isinstance(quality_answer, ScoreAnswer)
        resolves, unsupported, quality = resolves_p, unsupported_p, quality_answer
        claim_results: list[dict[str, Any]] = [
            {"text": text, "supported": round(p, 3)}
            for text, p in zip(claims, claim_ps, strict=True) if p is not None]
        unsupported_claims = [c for c in claim_results if c["supported"] < 0.5]
        passed = (resolves >= self.settings.verify_min_resolves
                  and quality.score >= self.settings.verify_min_quality and unsupported < 0.5
                  and not unsupported_claims)
        verification = {"resolves": round(resolves, 3), "quality": round(quality.score, 2),
                        "unsupported_claims": round(unsupported, 3), "claims": claim_results,
                        "passed": passed, "calibrated": response.calibrated}
        claim_note = (f", fatos sem base {len(unsupported_claims)}/{len(claims)}" if claims else "")
        notes = state.get("notes", []) + ([fallback_note] if fallback_note else []) + [
            (f"verificação: resolve {resolves:.2f}, qualidade {quality.score:.1f}, "
             f"afirmações sem base {unsupported:.2f}{claim_note} -> {'ok' if passed else 'reprovada'}")]
        log_event("verify", item_id=state["item_id"], passed=passed, resolves=round(resolves, 2),
                  quality=round(quality.score, 1), unsupported=round(unsupported, 2),
                  unsupported_claims=len(unsupported_claims))
        update: dict[str, Any] = {"verification": verification, "notes": notes}
        if passed:
            update["status"] = "verified"
            return update
        if state.get("regenerations", 0) < self.settings.max_regenerations:
            feedback = ("A resposta anterior foi reprovada na verificação. "
                        f"Ela resolve o pedido? p={resolves:.2f}. Qualidade {quality.score:.1f}/5. "
                        f"Contém afirmações sem base nos dados? p={unsupported:.2f}. ")
            if unsupported_claims:
                feedback += ("Estes fatos NÃO têm base nos dados obtidos; remova-os ou corrija-os: "
                             + "; ".join(f"'{c['text']}' (p={c['supported']:.2f})"
                                         for c in unsupported_claims) + ". ")
            feedback += "Reescreva usando somente os dados obtidos pelas ferramentas."

            messages.append(HumanMessage(content=feedback))
            update.update({"messages": messages_to_dict(messages), "draft_reply": "",
                           "regenerations": state.get("regenerations", 0) + 1, "status": "regenerate"})
            return update
        update.update({"status": "escalated",
                       "escalation_reason": "resposta reprovada na verificação após regenerar"})
        return update

    def request_approval(self, state: AgentState) -> dict[str, Any]:
        email = state["email"]
        pending = state.get("pending_action")
        if pending:
            call = pending["call"]
            kind = "tool"
            action = {"tool": call["name"], "args": call["args"], "reason": pending["reason"]}
            text = (f"🔐 Aprovação necessária — item {state['item_id']}\n"
                    f"Cliente: {email.get('from_name')} <{email.get('from_addr')}>\n"
                    f"Assunto: {email.get('subject')}\n\nAção: {call['name']}\n"
                    f"Argumentos: {json.dumps(call['args'], ensure_ascii=False)}\n"
                    f"Motivo: {pending['reason']}")
        else:
            kind = "send"
            action = {"draft_reply": state.get("draft_reply", "")}
            text = (f"✉️ Revisar resposta — item {state['item_id']} (confiança média)\n"
                    f"Cliente: {email.get('from_name')} <{email.get('from_addr')}>\n"
                    f"Assunto: {email.get('subject')}\n\n--- Rascunho ---\n{state.get('draft_reply', '')}")

        approval_id = self.store.create_approval(state["item_id"], kind, action, None)
        log_event("approval_requested", item_id=state["item_id"], approval_id=approval_id, kind=kind,
                  tool=action.get("tool"))
        buttons = [Button(text="✅ Aprovar", callback_data=f"approve:{approval_id}"),
                   Button(text="❌ Rejeitar", callback_data=f"reject:{approval_id}")]
        message_id = self.adapters.telegram.send_message(text, buttons)
        self.store.set_approval_message(approval_id, message_id)

        telegram = self.adapters.telegram
        if isinstance(telegram, MockTelegramAdapter) and telegram.auto_approve:
            self.store.decide_approval(approval_id, True, "mock-auto")
        return {"status": "awaiting_approval",
                "notes": state.get("notes", []) + [f"aprovação #{approval_id} ({kind}) solicitada"]}

    def send(self, state: AgentState) -> dict[str, Any]:
        """Envio no máximo uma vez.

        Grava o marcador `sending` em disco antes do efeito externo. Se o processo morrer entre o
        envio e a gravação final, o item é encontrado em `sending` na retomada e vai para um humano
        confirmar, em vez de reenviar.
        """
        if state.get("sent_at"):
            return {"status": "sent", "approval": None,
                    "notes": state.get("notes", []) + [f"envio ignorado: já enviado em {state['sent_at']}"]}
        email_dict = state["email"]
        original = EmailMessage(**email_dict)
        self.store.set_item_state(state["item_id"], "sending", {**dict(state), "status": "sending"})

        sent_message_id = self.adapters.email.send_reply(original, state.get("draft_reply", ""))
        if sent_message_id:
            self.store.set_sent_message_id(state["item_id"], sent_message_id)
        for fwd in state.get("forwarded", []):
            self.adapters.email.forward(original, fwd["to"], fwd["note"])
        sent_at = datetime.now(UTC).isoformat(timespec="seconds")

        log_event("sent", item_id=state["item_id"], tier=state.get("tier"),
                  category=state.get("triage", {}).get("category"))
        update = {"status": "sent", "approval": None, "sent_at": sent_at,
                  "sent_message_id": sent_message_id or "",
                  "notes": state.get("notes", []) + ["e-mail enviado"]}
        self.store.set_item_state(state["item_id"], "sent", {**dict(state), **update})
        self.adapters.telegram.send_message(
            f"✅ Respondido — item {state['item_id']} | {email_dict.get('subject')} | "
            f"{state.get('triage', {}).get('category')} | tier {state.get('tier')}",
            self._label_buttons(state["item_id"]))
        return update

    def escalate(self, state: AgentState) -> dict[str, Any]:
        email = state["email"]
        reason = state.get("escalation_reason") or "revisão humana"
        log_event("escalated", item_id=state["item_id"], reason=reason)
        category = state.get("triage", {}).get("category")
        self.adapters.telegram.send_message(
            f"🙋 Escalado para humano — item {state['item_id']}\n"
            f"Cliente: {email.get('from_name')} <{email.get('from_addr')}>\n"
            f"Assunto: {email.get('subject')}\nCategoria: {category or 'sem triagem'}\nMotivo: {reason}"
            + (f"\n\n--- Último rascunho ---\n{state['draft_reply']}" if state.get("draft_reply") else ""),
            self._label_buttons(state["item_id"]) if category else None)
        return {"status": "escalated", "approval": None,
                "notes": state.get("notes", []) + [f"escalado: {reason}"]}

    def finalize(self, state: AgentState) -> dict[str, Any]:
        if state.get("status") == "discarded":
            self.adapters.telegram.send_message(
                f"🗑️ Descartado como spam — item {state['item_id']} | {state['email'].get('subject')}",
                self._label_buttons(state["item_id"]))
        return {}
