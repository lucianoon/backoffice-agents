from datetime import UTC, datetime, timedelta

from conftest import ingest_only
from langchain_core.messages import AIMessage

from backoffice_agents.retention import REDACTED, purge


def _age(store, item_id, days):
    old = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    from sqlalchemy import text
    with store.engine.begin() as conn:
        conn.execute(text("UPDATE work_items SET updated_at=:t WHERE id=:i"), {"t": old, "i": item_id})


def test_redact_then_delete_by_age(make_runner, settings):
    settings.retention_redact_days = 30
    settings.retention_delete_days = 180
    runner = make_runner([AIMessage(content="ok\nEquipe de Atendimento")])
    ingest_only(runner, "email:em-001")
    runner.process_item("email:em-001")
    store = runner.store

    assert purge(store, settings) .redacted == []                    # recente: intocado
    _age(store, "email:em-001", 45)
    result = purge(store, settings, dry_run=True)
    assert result.redacted == ["email:em-001"] and result.deleted == []
    assert store.get_item("email:em-001")["payload"]["body"] != REDACTED  # dry-run não altera

    result = purge(store, settings)
    assert result.redacted == ["email:em-001"]
    item = store.get_item("email:em-001")
    assert item["payload"]["body"] == REDACTED and item["payload"]["from_addr"] == REDACTED
    assert item["payload"]["attachments"] == [] and item["message_id"] is None
    assert item["state"]["redacted"] is True and item["state"]["triage"]["category"] == "status_pedido"
    assert "messages" not in item["state"] and "draft_reply" not in item["state"]
    assert "claims" not in item["state"]["verification"]
    assert item["redacted_at"]
    assert store.list_decisions("email:em-001")                       # calibração preservada
    assert purge(store, settings).redacted == []                      # não redige duas vezes

    _age(store, "email:em-001", 200)
    result = purge(store, settings)
    assert result.deleted == ["email:em-001"]
    assert store.get_item("email:em-001") is None
    assert store.list_decisions("email:em-001") == [] and store.list_model_calls("email:em-001") == []


def test_open_items_are_never_purged(make_runner, settings):
    runner = make_runner([])
    runner.ingest_emails()
    for item in runner.store.list_items():
        _age(runner.store, item["id"], 400)
    result = purge(runner.store, settings)
    assert result.deleted == [] and result.redacted == []
    assert len(runner.store.list_items("new")) == 7
