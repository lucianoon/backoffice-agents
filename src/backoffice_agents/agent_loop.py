"""Loop de tool-calling com gate antes de cada execução.

Escrito à mão (em vez do agente pronto do LangGraph) para que o gate do Jev
fique no caminho crítico e o loop possa parar e retomar após aprovação humana.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from .policy import GateOutcome
from .tools import ToolRegistry

GateFn = Callable[[dict[str, Any]], tuple[GateOutcome, str]]
LlmCallHook = Callable[[AIMessage, float], None]  # (resposta, latência em ms)


@dataclass
class AgentTurn:
    messages: list[BaseMessage]
    final_text: str | None = None
    pending_call: dict[str, Any] | None = None
    pending_reason: str = ""
    escalated: bool = False


def unanswered_tool_calls(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """Tool calls da última AIMessage que ainda não têm ToolMessage correspondente."""
    last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    if last_ai is None or not last_ai.tool_calls:
        return []
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    return [c for c in last_ai.tool_calls if c["id"] not in answered]


def execute_tool(registry: ToolRegistry, call: dict[str, Any]) -> ToolMessage:
    tool = registry.by_name(call["name"])
    try:
        result = tool.invoke(call["args"])
    except Exception as exc:  # erro da ferramenta volta para o LLM decidir o que fazer
        result = {"ok": False, "error": str(exc)}
    return ToolMessage(content=json.dumps(result, ensure_ascii=False, default=str),
                       tool_call_id=call["id"], name=call["name"])


def run_agent(llm: BaseChatModel, registry: ToolRegistry, messages: list[BaseMessage],
              gate: GateFn, max_iterations: int, on_llm_call: LlmCallHook | None = None) -> AgentTurn:
    llm_with_tools = llm.bind_tools(registry.tools)
    messages = list(messages)

    for _ in range(max_iterations):
        calls = unanswered_tool_calls(messages)
        if not calls:
            started = time.perf_counter()
            ai = llm_with_tools.invoke(messages)
            if on_llm_call:
                on_llm_call(ai, (time.perf_counter() - started) * 1000)
            messages.append(ai)
            if not ai.tool_calls:
                return AgentTurn(messages=messages, final_text=_text(ai))
            calls = list(ai.tool_calls)

        for call in calls:
            outcome, reason = gate(call)
            if outcome == GateOutcome.APPROVE:
                return AgentTurn(messages=messages, pending_call=call, pending_reason=reason)
            if outcome == GateOutcome.REJECT:
                messages.append(ToolMessage(
                    content=json.dumps({"rejected": True, "reason": reason}, ensure_ascii=False),
                    tool_call_id=call["id"], name=call["name"]))
                continue
            messages.append(execute_tool(registry, call))
            if registry.context.escalation_reason:
                return AgentTurn(messages=messages, escalated=True)

    return AgentTurn(messages=messages, escalated=True)


def _text(message: AIMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content.strip()
    return "".join(part.get("text", "") for part in content if isinstance(part, dict)).strip()
