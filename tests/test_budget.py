import json

from conftest import ingest_only
from langchain_core.messages import AIMessage

from backoffice_agents.budget import OMITTED, estimate_tokens, fit_state


def _big_state(n_history=10, att_chars=20_000, facts=8, fact_chars=5_000, body_chars=10_000):
    return {
        "email": {"from_addr": "a@b.c", "subject": "s", "body": "x" * body_chars},
        "conversation_history": [{"date": str(i), "customer_message": "m" * 800, "our_reply": "r" * 800}
                                 for i in range(n_history)],
        "attachments": [{"filename": "a.pdf", "text": "t" * att_chars}],
        "data_gathered": [{"tool": "erp_get_order", "result": {"blob": "d" * fact_chars}}
                          for _ in range(facts)],
    }


def test_small_state_is_untouched():
    state = {"email": {"body": "oi"}, "conversation_history": [{"customer_message": "x"}]}
    fitted, cuts = fit_state(state, 1000)
    assert fitted == state and cuts == []


def test_cuts_least_important_first_and_keeps_original():
    state = _big_state()
    before = json.dumps(state)
    fitted, cuts = fit_state(state, 8000)
    assert json.dumps(state) == before                       # original intacto
    assert estimate_tokens(fitted) <= 8000
    assert cuts[0].startswith("histórico")                   # histórico cai antes de anexos e fatos
    assert len(fitted["conversation_history"]) <= 3
    assert fitted["email"]["body"].startswith("x")           # corpo preservado (pelo menos o início)


def test_extreme_budget_omits_history_and_attachments_but_keeps_request():
    fitted, cuts = fit_state(_big_state(), 900)
    assert estimate_tokens(fitted) <= 900
    assert "conversation_history" not in fitted
    assert fitted["attachments"][0]["text"] == OMITTED
    assert "customer_request" not in fitted and fitted["email"]["body"]   # e-mail continua lá, cortado
    assert any("omitid" in c for c in cuts)


def test_brute_force_fallback_when_steps_are_not_enough():
    state = {"email": {"body": "y" * 50_000}}                 # só o corpo, maior que qualquer passo
    fitted, cuts = fit_state(state, 500)
    assert estimate_tokens(fitted) <= 500 or len(fitted["email"]["body"]) < 200
    assert any("corte bruto" in c for c in cuts)


def test_oversized_state_is_reduced_before_jev_and_noted(make_runner, settings):
    settings.jev_state_budget_tokens = 60          # menor que o e-mail de exemplo
    runner = make_runner([AIMessage(content="ok\nEquipe de Atendimento")])
    ingest_only(runner, "email:em-001")
    payload = dict(runner.store.get_item("email:em-001")["payload"], body="relato longo " * 500)
    runner.store.upsert_item("email:big", "email", "new", payload)
    final = runner.process_item("email:big")
    assert final["status"] in {"sent", "awaiting_approval"}
    assert any("estado reduzido" in n for n in final["notes"])
    sent_state = runner.jev.states[0]                # o que de fato foi ao Jev na triagem
    assert len(sent_state["email"]["body"]) < len(payload["body"])
    assert estimate_tokens(sent_state) <= 60 or len(sent_state["email"]["body"]) < 200
