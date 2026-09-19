"""Grafo LangGraph: triagem -> ação (com gate) -> verificação -> envio, com paradas para aprovação.

Retomada após aprovação humana: o estado persistido volta com `approval` preenchido e o
roteador de entrada pula direto para `resume_tool` ou `send`/`escalate`.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from ..policy import Tier
from .nodes import Nodes
from .state import AgentState


def _entry(state: AgentState) -> str:
    approval = state.get("approval")
    if not approval:
        return "triage"
    if approval.get("kind") == "tool":
        return "resume_tool"
    return "send" if approval.get("approved") else "escalate"


def _after_triage(state: AgentState) -> str:
    status = state.get("status")
    if status == "discarded":
        return "finalize"
    if status == "escalated":
        return "escalate"
    return "act"


def _after_act(state: AgentState) -> str:
    status = state.get("status")
    if status == "awaiting_approval":
        return "request_approval"
    if status == "escalated":
        return "escalate"
    return "verify"


def _after_verify(state: AgentState) -> str:
    status = state.get("status")
    if status == "regenerate":
        return "act"
    if status == "escalated":
        return "escalate"
    return "send" if state.get("tier") == Tier.AUTO else "request_approval"


def build_graph(nodes: Nodes):
    graph = StateGraph(AgentState)
    graph.add_node("triage", nodes.triage)
    graph.add_node("act", nodes.act)
    graph.add_node("resume_tool", nodes.resume_tool)
    graph.add_node("verify", nodes.verify)
    graph.add_node("request_approval", nodes.request_approval)
    graph.add_node("send", nodes.send)
    graph.add_node("escalate", nodes.escalate)
    graph.add_node("finalize", nodes.finalize)

    graph.add_conditional_edges(START, _entry,
                                {"triage": "triage", "resume_tool": "resume_tool", "send": "send",
                                 "escalate": "escalate"})
    graph.add_conditional_edges("triage", _after_triage,
                                {"finalize": "finalize", "escalate": "escalate", "act": "act"})
    graph.add_conditional_edges("act", _after_act,
                                {"request_approval": "request_approval", "escalate": "escalate",
                                 "verify": "verify"})
    graph.add_edge("resume_tool", "act")
    graph.add_conditional_edges("verify", _after_verify,
                                {"act": "act", "escalate": "escalate", "send": "send",
                                 "request_approval": "request_approval"})
    graph.add_edge("request_approval", END)
    graph.add_edge("send", "finalize")
    graph.add_edge("escalate", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()
