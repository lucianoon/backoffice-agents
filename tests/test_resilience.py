"""Pseudonimização, fallback do Jev, reprocessamento de erros e envio idempotente."""

import json

from conftest import FakeJev, ingest_only, tool_call
from langchain_core.messages import AIMessage

MARIANA = "email:em-001"
REPLY = "Olá, seu pedido PED-78231 foi enviado.\nEquipe de Atendimento"
HAPPY = [
    tool_call("erp_get_order", {"order_id": "PED-78231"}, "c1"),
    tool_call("crm_log_interaction", {"email": "mariana.souza@lojaazul.com.br", "summary": "ok"}, "c2"),
    AIMessage(content=REPLY),
]


def test_state_sent_to_jev_has_no_pii(make_runner):
    runner = make_runner(HAPPY)
    ingest_only(runner, MARIANA)
    runner.process_item(MARIANA)

    assert len(runner.jev.states) == 3  # triagem, gate, verificação
    dump = json.dumps(runner.jev.states, ensure_ascii=False)
    assert "mariana.souza@lojaazul.com.br" not in dump
    assert "Mariana" not in dump and "Souza" not in dump
    assert "<email_1>" in dump and "<nome_1>" in dump
    assert "PED-78231" in dump                      # identificadores de negócio continuam
    # o e-mail de fato enviado ao cliente não é afetado
    assert runner.adapters.email.sent[0].to == "mariana.souza@lojaazul.com.br"


def test_anonymization_can_be_disabled(make_runner, settings):
    settings.jev_anonymize = False
    runner = make_runner(HAPPY)
    ingest_only(runner, MARIANA)
    runner.process_item(MARIANA)
    assert "mariana.souza@lojaazul.com.br" in json.dumps(runner.jev.states)


def test_triage_uses_fallback_when_jev_is_down(make_runner):
    primary = FakeJev(fail_first=1)
    fallback = FakeJev()
    runner = make_runner(HAPPY, jev=primary, jev_fallback=fallback)
    ingest_only(runner, MARIANA)
    final = runner.process_item(MARIANA)

    assert final["status"] == "sent"
    assert any("fallback emulado" in n for n in final["notes"])
    assert len(fallback.calls) == 1                     # só a triagem caiu no fallback
    stages = {d["stage"]: d["model"] for d in runner.store.list_decisions(MARIANA)}
    assert stages["triage"] == "fake-jev"


def test_triage_without_fallback_escalates_instead_of_crashing(make_runner):
    runner = make_runner(HAPPY, jev=FakeJev(fail_first=99))
    ingest_only(runner, MARIANA)
    final = runner.process_item(MARIANA)
    assert final["status"] == "escalated"
    assert "triagem indisponível" in final["escalation_reason"]
    assert runner.llm.calls == 0 and runner.adapters.email.sent == []


def test_verification_failure_escalates_with_draft(make_runner):
    runner = make_runner(HAPPY)
    ingest_only(runner, MARIANA)
    runner.jev.fail_calls = {3}        # triagem e gate passam (chamadas 1 e 2); verificação (3) falha
    final = runner.process_item(MARIANA)
    assert final["status"] == "escalated"
    assert "verificação indisponível" in final["escalation_reason"]
    assert final["draft_reply"] == REPLY
    assert runner.adapters.email.sent == []


def test_error_items_are_retried_then_failed(make_runner, settings):
    settings.max_attempts = 2
    runner = make_runner([])           # LLM sem roteiro: explode no nó act
    ingest_only(runner, MARIANA)

    assert runner.run_pending() == [(MARIANA, "error")]
    assert runner.store.get_item(MARIANA)["attempts"] == 1
    assert runner.run_pending() == [(MARIANA, "failed")]
    assert runner.run_pending() == []                   # failed é terminal
    assert any("Falhou após 2" in m["text"] for m in runner.adapters.telegram.sent)


def test_send_is_idempotent_on_resume(make_runner):
    runner = make_runner(HAPPY)
    ingest_only(runner, MARIANA)
    final = runner.process_item(MARIANA)
    assert final["sent_at"]

    # simula uma retomada indevida do nó de envio (ex.: aprovação duplicada)
    again = runner.graph.invoke({**final, "approval": {"id": 99, "kind": "send", "approved": True}})
    assert again["status"] == "sent"
    assert len(runner.adapters.email.sent) == 1
    assert any("já enviado" in n for n in again["notes"])


def test_item_stuck_in_sending_is_escalated_not_resent(make_runner):
    runner = make_runner(HAPPY)
    ingest_only(runner, MARIANA)
    item = runner.store.get_item(MARIANA)
    runner.store.set_item_state(MARIANA, "sending", {"item_id": MARIANA, "email": item["payload"],
                                                     "draft_reply": REPLY, "status": "sending", "notes": []})
    assert runner.run_pending() == [(MARIANA, "escalated")]
    assert runner.adapters.email.sent == []
    assert "confirmar se a resposta saiu" in runner.store.get_item(MARIANA)["state"]["escalation_reason"]


def test_approval_stuck_in_applying_is_escalated(make_runner):
    runner = make_runner(HAPPY)
    ingest_only(runner, MARIANA)
    item = runner.store.get_item(MARIANA)
    runner.store.set_item_state(MARIANA, "awaiting_approval",
                                {"item_id": MARIANA, "email": item["payload"], "status": "awaiting_approval",
                                 "notes": []})
    approval_id = runner.store.create_approval(MARIANA, "tool", {"tool": "erp_cancel_order"}, None)
    runner.store.set_approval_status(approval_id, "applying")

    assert runner.run_pending() == [(MARIANA, "escalated")]
    assert runner.store.get_approval(approval_id)["status"] == "applied"
    assert runner.adapters.erp.get_order("PED-78410").status == "aguardando_pagamento"
