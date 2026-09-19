"""Ponta a ponta com mocks: LLM roteirizado + Jev falso + adapters mock."""

from langchain_core.messages import AIMessage

from conftest import ingest_only, tool_call

MARIANA = "email:em-001"   # status de pedido, cliente conhecida
ANA_CANCEL = "email:em-004"
SPAM = "email:em-005"
ROBERTO = "email:em-006"

REPLY = "Olá Mariana, seu pedido PED-78231 foi enviado (rastreio BR123456789XX)...\nEquipe de Atendimento"


def test_happy_path_reads_erp_logs_crm_and_sends(make_runner):
    runner = make_runner([
        tool_call("erp_get_order", {"order_id": "PED-78231"}, "c1"),
        tool_call("crm_log_interaction", {"email": "mariana.souza@lojaazul.com.br",
                                          "summary": "Cliente pediu rastreio; informado."}, "c2"),
        AIMessage(content=REPLY),
    ])
    runner.ingest_emails()
    final = runner.process_item(MARIANA)

    assert final["status"] == "sent"
    assert final["tier"] == "auto"
    assert final["verification"]["passed"] is True
    sent = runner.adapters.email.sent
    assert len(sent) == 1 and sent[0].to == "mariana.souza@lojaazul.com.br" and sent[0].body == REPLY
    assert len(runner.adapters.crm.interactions) == 1

    stages = {d["stage"] for d in runner.store.list_decisions(MARIANA)}
    assert stages == {"triage", "gate:crm_log_interaction", "verify"}


def test_critical_tool_waits_for_human_then_resumes(make_runner):
    runner = make_runner([
        tool_call("erp_cancel_order", {"order_id": "PED-78410", "reason": "comprou errado"}, "c1"),
        AIMessage(content="Olá Ana, seu pedido PED-78410 foi cancelado.\nEquipe de Atendimento"),
    ], jev_overrides={"category": ("cancelamento", 0.9), "needs_human": 0.3})
    ingest_only(runner, ANA_CANCEL)

    first = runner.process_item(ANA_CANCEL)
    assert first["status"] == "awaiting_approval"
    assert first["pending_action"]["call"]["name"] == "erp_cancel_order"
    assert runner.adapters.erp.get_order("PED-78410").status == "aguardando_pagamento"  # nada executado

    # o mock do Telegram auto-aprovou; run_pending retoma
    outcomes = runner.run_pending()
    assert outcomes == [(ANA_CANCEL, "sent")]
    assert runner.adapters.erp.get_order("PED-78410").status == "cancelado"
    approval = runner.store.latest_approval_for_item(ANA_CANCEL)
    assert approval["status"] == "applied" and approval["decided_by"] == "mock-auto"


def test_rejected_action_goes_back_to_llm(make_runner, settings):
    settings.telegram_mock_auto_approve = False
    runner = make_runner([
        tool_call("erp_cancel_order", {"order_id": "PED-78410", "reason": "x"}, "c1"),
        tool_call("escalate_to_human", {"reason": "operador não autorizou o cancelamento"}, "c2"),
    ], jev_overrides={"category": ("cancelamento", 0.9)})
    runner.ingest_emails()
    runner.process_item(ANA_CANCEL)
    approval = runner.store.list_approvals("pending")[0]
    runner.store.decide_approval(approval["id"], False, "teste")

    final = runner.resume_item(runner.store.get_approval(approval["id"]))
    assert final["status"] == "escalated"
    assert "não autorizou" in final["escalation_reason"]
    assert runner.adapters.erp.get_order("PED-78410").status == "aguardando_pagamento"


def test_spam_is_discarded_without_calling_llm(make_runner):
    runner = make_runner([], jev_overrides={"category": ("spam_irrelevante", 0.97)})
    runner.ingest_emails()
    final = runner.process_item(SPAM)
    assert final["status"] == "discarded"
    assert runner.llm.calls == 0


def test_needs_human_escalates_before_acting(make_runner):
    runner = make_runner([], jev_overrides={"category": ("reclamacao", 0.9), "needs_human": 0.93})
    runner.ingest_emails()
    final = runner.process_item(ROBERTO)
    assert final["status"] == "escalated"
    assert runner.llm.calls == 0
    assert any("Escalado" in m["text"] for m in runner.adapters.telegram.sent)


def test_failed_verification_regenerates_once(make_runner):
    runner = make_runner([
        AIMessage(content="Seu pedido chega amanhã com certeza."),      # inventa prazo
        AIMessage(content="Seu pedido PED-78231 foi enviado; previsão 22/09.\nEquipe de Atendimento"),
    ], jev_overrides={"unsupported_claims": lambda n: 0.9 if n == 0 else 0.05})
    runner.ingest_emails()
    final = runner.process_item(MARIANA)
    assert final["status"] == "sent"
    assert final["regenerations"] == 1
    assert runner.llm.calls == 2


def test_medium_confidence_sends_draft_for_review(make_runner):
    runner = make_runner([AIMessage(content="Resposta razoável.\nEquipe de Atendimento")],
                         jev_overrides={"category": ("status_pedido", 0.7)})
    ingest_only(runner, MARIANA)
    first = runner.process_item(MARIANA)
    assert first["tier"] == "review" and first["status"] == "awaiting_approval"
    assert runner.store.latest_approval_for_item(MARIANA)["kind"] == "send"
    assert runner.run_pending() == [(MARIANA, "sent")]
