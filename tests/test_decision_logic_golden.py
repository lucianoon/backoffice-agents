"""Tabela-verdade da lógica determinística de decisão, independente do cassete de replay.

O replay do CI (`eval-shadow --replay`) usa um cassete gerado a partir dos rótulos: ele pega
prompt/taxonomia/dataset desatualizados, não regressão de lógica. Estes casos rotulados fixam o que
o código faz com as probabilidades do Jev: precedência dos escalonamentos da triagem, risco de cada
ferramenta, desfecho do gate e aprovação/regeneração na verificação. Mudar limiar default, ordem
dos checks ou nível de risco quebra esta tabela de propósito.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FakeJev, ScriptedLLM

from backoffice_agents.adapters import build_adapters
from backoffice_agents.config import Settings
from backoffice_agents.graph.nodes import Nodes
from backoffice_agents.graph.workflow import _after_triage, _after_verify
from backoffice_agents.policy import GateOutcome, RiskLevel, gate_outcome
from backoffice_agents.storage import Store
from backoffice_agents.tools import ToolContext, build_tools

ROOT = Path(__file__).resolve().parents[1]
EMAIL = json.loads((ROOT / "data" / "samples" / "emails.json").read_text(encoding="utf-8"))[0]


@pytest.fixture
def nodes_for(tmp_path):
    def _make(overrides: dict) -> Nodes:
        settings = Settings(_env_file=None, db_url="sqlite:///" + (tmp_path / "g.db").as_posix(),
                            kb_dir=str(tmp_path / "sem_kb"))
        return Nodes(settings, ScriptedLLM(script=[]), FakeJev(overrides), build_adapters(settings),
                     Store(settings.db_url))
    return _make


# (id, respostas do Jev na triagem, status, tier, trecho do motivo, próximo nó)
TRIAGE_CASES = [
    ("auto", {}, "triaged", "auto", None, "act"),
    ("review", {"category": ("status_pedido", 0.70)}, "triaged", "review", None, "act"),
    ("baixa-confianca", {"category": ("status_pedido", 0.40)}, "escalated", "escalate",
     "confiança baixa", "escalate"),
    ("injection", {"injection": 0.70}, "escalated", "escalate", "prompt injection", "escalate"),
    ("injection-abaixo", {"injection": 0.69}, "triaged", "auto", None, "act"),
    ("sensivel", {"sensitive": 0.75}, "escalated", "escalate", "dado sensível", "escalate"),
    ("humano", {"needs_human": 0.70}, "escalated", "escalate", "exige humano", "escalate"),
    ("spam-alto", {"category": ("spam_irrelevante", 0.95)}, "discarded", "escalate", None, "finalize"),
    ("spam-medio", {"category": ("spam_irrelevante", 0.70)}, "triaged", "review", None, "act"),
    # precedência: injection > sensível > spam > humano > confiança
    ("injection-vence-tudo", {"injection": 0.9, "sensitive": 0.9, "needs_human": 0.9,
                              "category": ("spam_irrelevante", 0.99)},
     "escalated", "escalate", "prompt injection", "escalate"),
    ("sensivel-vence-spam", {"sensitive": 0.9, "category": ("spam_irrelevante", 0.99)},
     "escalated", "escalate", "dado sensível", "escalate"),
    ("spam-vence-humano", {"needs_human": 0.9, "category": ("spam_irrelevante", 0.99)},
     "discarded", "escalate", None, "finalize"),
    ("humano-vence-confianca", {"needs_human": 0.9, "category": ("status_pedido", 0.30)},
     "escalated", "escalate", "exige humano", "escalate"),
]


@pytest.mark.parametrize(("case", "answers", "status", "tier", "reason", "next_node"), TRIAGE_CASES,
                         ids=[c[0] for c in TRIAGE_CASES])
def test_triage_routing(nodes_for, case, answers, status, tier, reason, next_node):
    nodes = nodes_for(answers)
    out = nodes.triage({"item_id": f"email:{case}", "email": EMAIL, "thread_id": f"email:{case}"})
    assert (out["status"], str(out["tier"])) == (status, tier)
    assert (reason in out["escalation_reason"]) if reason else "escalation_reason" not in out
    assert ("messages" in out) == (status == "triaged")   # só item triado recebe prompt do agente
    assert nodes.llm.calls == 0                           # triagem nunca chama o LLM (sem imagens)
    assert _after_triage(out) == next_node


EXPECTED_RISK = {
    "crm_find_contact": RiskLevel.LOW, "crm_open_deals": RiskLevel.LOW,
    "crm_log_interaction": RiskLevel.MEDIUM, "crm_create_deal": RiskLevel.HIGH,
    "erp_get_order": RiskLevel.LOW, "erp_list_orders": RiskLevel.LOW,
    "erp_get_invoice": RiskLevel.LOW, "erp_list_invoices": RiskLevel.LOW,
    "erp_check_stock": RiskLevel.LOW, "erp_create_order": RiskLevel.HIGH,
    "erp_cancel_order": RiskLevel.CRITICAL, "email_forward": RiskLevel.MEDIUM,
    "escalate_to_human": RiskLevel.LOW, "kb_search": RiskLevel.LOW,
}


def test_tool_risk_levels_are_pinned():
    registry = build_tools(build_adapters(Settings(_env_file=None)), ToolContext(customer_email="x@y.z"))
    assert registry.risk == EXPECTED_RISK


# (risco, appropriate, args_complete, desfecho)
GATE_CASES = [
    (RiskLevel.LOW, None, None, GateOutcome.EXECUTE),
    (RiskLevel.HIGH, 0.99, 0.99, GateOutcome.APPROVE),
    (RiskLevel.CRITICAL, 0.99, 0.99, GateOutcome.APPROVE),
    (RiskLevel.MEDIUM, None, 0.9, GateOutcome.APPROVE),
    (RiskLevel.MEDIUM, 0.49, 0.99, GateOutcome.REJECT),
    (RiskLevel.MEDIUM, 0.99, 0.49, GateOutcome.REJECT),
    (RiskLevel.MEDIUM, 0.69, 0.99, GateOutcome.APPROVE),
    (RiskLevel.MEDIUM, 0.70, 0.70, GateOutcome.EXECUTE),
]


@pytest.mark.parametrize(("risk", "appropriate", "args_complete", "outcome"), GATE_CASES)
def test_gate_outcome(risk, appropriate, args_complete, outcome):
    assert gate_outcome(risk, appropriate, args_complete, Settings(_env_file=None))[0] == outcome


# (id, respostas do Jev na verificação, tier, regenerações já feitas, status, próximo nó)
VERIFY_CASES = [
    ("passa-auto", {}, "auto", 0, "verified", "send"),
    ("passa-review", {}, "review", 0, "verified", "request_approval"),
    ("nao-resolve", {"resolves": 0.69}, "auto", 0, "regenerate", "act"),
    ("qualidade-baixa", {"quality": 3.4}, "auto", 0, "regenerate", "act"),
    ("sem-base", {"unsupported_claims": 0.5}, "auto", 0, "regenerate", "act"),
    ("fato-sem-base", {"claim_0": 0.4}, "auto", 0, "regenerate", "act"),
    ("reprova-de-novo", {"resolves": 0.1}, "auto", 1, "escalated", "escalate"),
]


@pytest.mark.parametrize(("case", "answers", "tier", "regenerations", "status", "next_node"),
                         VERIFY_CASES, ids=[c[0] for c in VERIFY_CASES])
def test_verify_outcome(nodes_for, case, answers, tier, regenerations, status, next_node):
    nodes = nodes_for(answers)
    state = {"item_id": f"email:{case}", "email": EMAIL, "tier": tier, "messages": [],
             "draft_reply": "Seu pedido foi enviado.", "claims": ["pedido enviado"],
             "regenerations": regenerations, "notes": []}
    out = nodes.verify(state)
    assert out["status"] == status
    assert _after_verify(state | out) == next_node
