"""Fila com reserva atômica: dois workers não pegam o mesmo item nem a mesma aprovação."""

import os

import pytest

from backoffice_agents.storage import Store


def _two_stores(settings):
    return Store(settings.db_url, worker_id="w1"), Store(settings.db_url, worker_id="w2")


def test_claim_next_is_exclusive_and_ordered(settings):
    w1, w2 = _two_stores(settings)
    w1.upsert_item("a", "email", "new", {"subject": "a"})
    w1.upsert_item("b", "email", "new", {"subject": "b"})
    w1.upsert_item("c", "email", "error", {"subject": "c"})
    w1.increment_attempts("c")           # 1 tentativa: ainda elegível com max 3
    w1.upsert_item("d", "email", "sent", {"subject": "d"})

    first = w1.claim_next(max_attempts=3)
    second = w2.claim_next(max_attempts=3)
    third = w1.claim_next(max_attempts=3)
    assert [first["id"], second["id"], third["id"]] == ["a", "b", "c"]
    assert first["status"] == "processing" and first["claimed_by"] == "w1"
    assert second["claimed_by"] == "w2"
    assert w2.claim_next(max_attempts=3) is None      # nada mais elegível


def test_claim_next_respects_max_attempts_delay_and_exclude(settings):
    store = Store(settings.db_url)
    store.upsert_item("x", "email", "error", {"subject": "x"})
    store.increment_attempts("x")
    store.increment_attempts("x")
    assert store.claim_next(max_attempts=2) is None                       # esgotou as tentativas
    assert store.claim_next(max_attempts=3, retry_delay_s=3600) is None   # ainda no backoff
    assert store.claim_next(max_attempts=3, exclude={"x"}) is None        # excluído neste ciclo
    assert store.claim_next(max_attempts=3)["id"] == "x"


def test_claim_next_approval_moves_to_applying_once(settings):
    w1, w2 = _two_stores(settings)
    w1.upsert_item("i", "email", "awaiting_approval", {"subject": "i"})
    a1 = w1.create_approval("i", "tool", {"tool": "erp_cancel_order"}, None)
    a2 = w1.create_approval("i", "send", {"draft_reply": "..."}, None)
    w1.decide_approval(a1, True, "op")
    w1.decide_approval(a2, False, "op")

    first = w1.claim_next_approval()
    second = w2.claim_next_approval()
    assert (first["id"], first["decided_status"]) == (a1, "approved")
    assert (second["id"], second["decided_status"]) == (a2, "rejected")
    assert first["status"] == second["status"] == "applying"
    assert w1.claim_next_approval() is None


def test_run_pending_consumes_queue_with_claims(make_runner):
    from langchain_core.messages import AIMessage

    runner = make_runner([AIMessage(content="ok\nEquipe de Atendimento")] * 7)
    runner.ingest_emails()
    outcomes = runner.run_pending()
    assert len(outcomes) == 7
    assert all(item["claimed_by"] == runner.store.worker_id for item in runner.store.list_items())
    assert runner.run_pending() == []


def test_model_calls_are_logged_per_item(make_runner):
    from conftest import ingest_only
    from langchain_core.messages import AIMessage

    runner = make_runner([AIMessage(content="ok\nEquipe de Atendimento")])
    ingest_only(runner, "email:em-001")
    runner.process_item("email:em-001")
    calls = runner.store.list_model_calls("email:em-001")
    kinds = [(c["kind"], c["stage"]) for c in calls]
    assert kinds == [("jev", "triage"), ("llm", "act"), ("jev", "verify")]
    assert all(c["model"] == "fake-jev" for c in calls if c["kind"] == "jev")


@pytest.mark.skipif(not os.environ.get("TEST_DB_URL"), reason="defina TEST_DB_URL para testar no Postgres")
def test_postgres_claims(settings):
    store = Store(os.environ["TEST_DB_URL"], worker_id="pg")
    store.upsert_item("pg-1", "email", "new", {"subject": "pg"})
    claimed = store.claim_next(max_attempts=3)
    assert claimed is not None and claimed["status"] == "processing"
    store.set_item_state("pg-1", "sent", {})
