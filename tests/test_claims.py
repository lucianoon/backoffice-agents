"""Verificação por afirmação: o rascunho lista fatos e o Jev checa cada um."""

from conftest import FakeJev, ingest_only, tool_call
from langchain_core.messages import AIMessage, messages_from_dict

from backoffice_agents.claims import split_reply

MARIANA = "email:em-001"
DRAFT = """Olá Mariana,

Seu pedido PED-78231 foi enviado com o rastreio BR123456789XX e chega até 22/09.

Equipe de Atendimento
---FATOS---
- Pedido PED-78231 está com status enviado
- Código de rastreio BR123456789XX
- Previsão de entrega 22/09/2026
"""


def test_split_reply_parses_delimiter_and_bullets():
    reply, claims = split_reply(DRAFT)
    assert reply.endswith("Equipe de Atendimento") and "FATOS" not in reply
    assert claims == ["Pedido PED-78231 está com status enviado", "Código de rastreio BR123456789XX",
                      "Previsão de entrega 22/09/2026"]
    assert split_reply("sem delimitador") == ("sem delimitador", [])
    assert split_reply("texto\nFATOS:\n1) um\n2. dois\n* três") == ("texto", ["um", "dois", "três"])
    assert len(split_reply("x\n---FATOS---\n" + "\n".join(f"- f{i}" for i in range(20)))[1]) == 10


def test_claims_are_verified_one_by_one_and_pass(make_runner):
    runner = make_runner([tool_call("erp_get_order", {"order_id": "PED-78231"}, "c1"),
                          AIMessage(content=DRAFT)],
                         jev_overrides={"claim_0": 0.95, "claim_1": 0.9, "claim_2": 0.85})
    ingest_only(runner, MARIANA)
    final = runner.process_item(MARIANA)
    assert final["status"] == "sent"
    assert final["draft_reply"].endswith("Equipe de Atendimento")      # cliente não vê a lista
    assert runner.adapters.email.sent[0].body == final["draft_reply"]
    assert [c["supported"] for c in final["verification"]["claims"]] == [0.95, 0.9, 0.85]
    verify_questions = runner.jev.calls[-1]
    assert {"claim_0", "claim_1", "claim_2"} <= set(verify_questions)   # mesma chamada da verificação
    assert runner.jev.states[-1]["claims"]["claim_1"] == "Código de rastreio BR123456789XX"
    assert any("fatos sem base 0/3" in n for n in final["notes"])


def test_unsupported_claim_fails_verification_with_specific_feedback(make_runner):
    fixed = DRAFT.replace("e chega até 22/09", "").replace("- Previsão de entrega 22/09/2026\n", "")
    runner = make_runner([AIMessage(content=DRAFT), AIMessage(content=fixed)],
                         jev_overrides={"claim_2": lambda n: 0.1 if n == 0 else 0.9})
    ingest_only(runner, MARIANA)
    final = runner.process_item(MARIANA)
    assert final["status"] == "sent" and final["regenerations"] == 1
    feedback = messages_from_dict(final["messages"])
    feedback_text = next(m.content for m in feedback if "NÃO têm base" in str(m.content))
    assert "Previsão de entrega 22/09/2026" in feedback_text and "p=0.10" in feedback_text
    assert len(final["verification"]["claims"]) == 2                   # segunda versão tem 2 fatos
    assert any("fatos sem base 1/3" in n for n in final["notes"])


def test_reply_without_claims_still_works(make_runner):
    runner = make_runner([AIMessage(content="Olá.\nEquipe de Atendimento")], jev=FakeJev())
    ingest_only(runner, MARIANA)
    final = runner.process_item(MARIANA)
    assert final["status"] == "sent" and final["claims"] == [] and final["verification"]["claims"] == []
