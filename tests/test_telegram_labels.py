from conftest import ingest_only
from langchain_core.messages import AIMessage

from backoffice_agents.channels.telegram_bot import handle_callback, handle_text

MARIANA = "email:em-001"
SCRIPT = [AIMessage(content="Olá.\nEquipe de Atendimento")]


def _run(make_runner):
    runner = make_runner(SCRIPT)
    ingest_only(runner, MARIANA)
    runner.process_item(MARIANA)
    return runner


def test_final_notification_carries_label_buttons(make_runner):
    runner = _run(make_runner)
    final = [m for m in runner.adapters.telegram.sent if "Respondido" in m["text"]][0]
    assert [b["callback_data"] for b in final["buttons"]] == [f"lbl:ok:{MARIANA}", f"lbl:fix:{MARIANA}"]


def test_ok_button_confirms_predicted_category(make_runner):
    runner = _run(make_runner)
    text, buttons = handle_callback(runner, f"lbl:ok:{MARIANA}", "telegram:1")
    assert "confirmada" in text and buttons is None
    decision = [d for d in runner.store.list_decisions(MARIANA) if d["question_id"] == "category"][0]
    assert decision["human_label"] == "status_pedido" and decision["human_label_by"] == "telegram:1"


def test_fix_button_offers_categories_then_records_correction(make_runner):
    runner = _run(make_runner)
    text, buttons = handle_callback(runner, f"lbl:fix:{MARIANA}", "telegram:1")
    assert "categoria correta" in text
    assert any(b.callback_data == f"lbl:set:{MARIANA}:cancelamento" for b in buttons)

    text, _ = handle_callback(runner, f"lbl:set:{MARIANA}:cancelamento", "telegram:1")
    assert "corrigida" in text
    decision = [d for d in runner.store.list_decisions(MARIANA) if d["question_id"] == "category"][0]
    assert decision["human_label"] == "cancelamento"

    report = handle_text(runner, "/calibracao", "telegram:1")
    assert "category | fake-jev | default@1 | 1 | 0%" in report


def test_unknown_or_invalid_callbacks_are_safe(make_runner):
    runner = _run(make_runner)
    bad = handle_callback(runner, "lbl:set:email:em-001:categoria_inexistente", "x")
    assert bad[0] == "botão desconhecido"
    assert "não existe" in handle_callback(runner, "lbl:ok:email:nope", "x")[0]
    assert handle_callback(runner, "approve:abc", "x")[0] == "botão desconhecido"
