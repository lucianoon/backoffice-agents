from datetime import UTC, datetime, timedelta

from conftest import ingest_only
from langchain_core.messages import AIMessage

from backoffice_agents.metrics import alerts, cloudwatch_metric_data, collect_metrics, to_prometheus
from backoffice_agents.storage import Store


def test_metrics_from_a_processed_item(make_runner):
    runner = make_runner([AIMessage(content="ok\nEquipe de Atendimento")])
    ingest_only(runner, "email:em-001")
    runner.process_item("email:em-001")
    data = collect_metrics(runner.store, runner.settings)
    assert data["processed"] == 1 and data["errors"] == 0 and data["by_status"]["sent"] == 1
    assert data["latency"]["jev"]["count"] == 2 and data["latency"]["llm"]["count"] == 1
    assert data["queue_depth"] == 0 and data["pending_approvals"] == 0
    text = to_prometheus(data)
    assert "backoffice_queue_depth 0" in text and 'backoffice_items_by_status{status="sent"} 1' in text
    names = {m["MetricName"] for m in cloudwatch_metric_data(data)}
    assert {"QueueDepth", "PendingApprovals", "ModelLatencyP95"} <= names


def test_alerts_and_cooldown(make_runner, settings):
    settings.alert_approval_max_age_min = 30
    settings.alert_queue_depth = 1
    runner = make_runner([])
    store: Store = runner.store
    store.upsert_item("a", "email", "new", {"subject": "a"})
    store.upsert_item("b", "email", "new", {"subject": "b"})
    store.upsert_item("c", "email", "awaiting_approval", {"subject": "c"})
    approval_id = store.create_approval("c", "send", {"draft_reply": "x"}, None)
    old = (datetime.now(UTC) - timedelta(minutes=90)).isoformat(timespec="seconds")
    with store.engine.begin() as conn:
        from sqlalchemy import text
        conn.execute(text("UPDATE approvals SET requested_at=:t WHERE id=:i"), {"t": old, "i": approval_id})

    data = collect_metrics(store, settings)
    assert data["queue_depth"] == 2 and data["oldest_pending_approval_min"] >= 89
    found = alerts(data, settings)
    assert len(found) == 2 and any("aprovacao pendente" in f for f in found)

    fired = runner.check_alerts()
    assert len(fired) == 2
    assert sum(1 for m in runner.adapters.telegram.sent if m["text"].startswith("Alerta")) == 2
    assert runner.check_alerts() == []                       # cooldown: não repete
