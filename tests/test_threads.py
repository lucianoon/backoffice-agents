"""Contexto de conversa: ligação de threads e histórico no prompt e na verificação."""

from conftest import ingest_only
from langchain_core.messages import AIMessage, messages_from_dict

from backoffice_agents.adapters.email import EmailMessage
from backoffice_agents.storage import normalize_subject
from backoffice_agents.threads import format_history, resolve_thread, thread_history

MARIANA = "email:em-001"
FOLLOW_UP = "email:em-007"     # "Re: Pedido PED-78231 - previsão de entrega?" da mesma remetente
REPLY = "Olá, enviado com rastreio BR123456789XX.\nEquipe de Atendimento"


def test_normalize_subject_strips_reply_prefixes():
    assert normalize_subject("RE: Res: Fwd:  Pedido   PED-1 ") == "pedido ped-1"
    assert normalize_subject("Enc: ENC: Cotação") == "cotação"
    assert normalize_subject("") == ""


def test_follow_up_by_subject_joins_thread_and_gets_history(make_runner):
    runner = make_runner([AIMessage(content=REPLY), AIMessage(content="Ok.\nEquipe de Atendimento")])
    runner.ingest_emails()
    first = runner.store.get_item(MARIANA)
    follow = runner.store.get_item(FOLLOW_UP)
    assert first["thread_id"] == MARIANA and follow["thread_id"] == MARIANA

    for item in runner.store.list_items("new"):
        if item["id"] not in {MARIANA, FOLLOW_UP}:
            runner.store.set_item_state(item["id"], "skipped", {})
    runner.process_item(MARIANA)
    final = runner.process_item(FOLLOW_UP)

    assert len(final["thread"]) == 1
    assert final["thread"][0]["our_reply"] == REPLY and final["thread"][0]["open"] is False
    prompt = messages_from_dict(final["messages"])[1].content
    assert "HISTÓRICO DA CONVERSA" in prompt and "BR123456789XX" in prompt
    assert any("thread com 1 mensagem" in n for n in final["notes"])
    # a verificação também enxerga a conversa (estado pseudonimizado)
    verify_state = runner.jev.states[-1]
    assert "conversation_history" in verify_state


def test_reply_to_our_message_id_links_by_header(make_runner):
    runner = make_runner([AIMessage(content=REPLY)])
    ingest_only(runner, MARIANA)
    final = runner.process_item(MARIANA)
    sent_id = final["sent_message_id"]
    assert sent_id and runner.store.get_item(MARIANA)["sent_message_id"] == sent_id

    reply = EmailMessage(id="x9", from_addr="outro@dominio.com", subject="assunto totalmente diferente",
                         body="respondendo", in_reply_to=sent_id)
    assert resolve_thread(runner.store, reply, "email:x9", 14) == MARIANA


def test_unrelated_email_opens_new_thread(make_runner):
    runner = make_runner([])
    ingest_only(runner, MARIANA)
    other = EmailMessage(id="y1", from_addr="mariana.souza@example.invalid", subject="Outro assunto",
                         body="?")
    assert resolve_thread(runner.store, other, "email:y1", 14) == "email:y1"
    old = EmailMessage(id="y2", from_addr="mariana.souza@example.invalid",
                       subject="Re: Pedido PED-78231 - previsão de entrega?", body="?")
    # janela negativa = nada é recente o bastante: fora da janela abre thread nova
    assert resolve_thread(runner.store, old, "email:y2", window_days=-1) == "email:y2"


def test_history_marks_open_and_escalated_items(make_runner):
    runner = make_runner([])
    runner.ingest_emails()
    runner.store.set_item_state(MARIANA, "awaiting_approval", {"triage": {"category": "status_pedido"}})
    history = thread_history(runner.store, MARIANA, exclude_item=FOLLOW_UP)
    assert history[0]["open"] is True and history[0]["category"] == "status_pedido"
    runner.store.set_item_state(MARIANA, "escalated", {})
    history = thread_history(runner.store, MARIANA, exclude_item=FOLLOW_UP)
    assert "escalado" in history[0]["our_reply"]
    assert "EM ABERTO" not in format_history(history)
