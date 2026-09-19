import os

from backoffice_agents.config import Settings
from backoffice_agents.tracing import configure_tracing, run_config, traced_jev_ask


def test_run_config_names_trace_by_item():
    s = Settings(_env_file=None, tracing="none", jev_mode="emulated")
    config = run_config(s, "email:em-001", {"resume": False})
    assert config["run_name"] == "backoffice email:em-001"
    assert "jev:emulated" in config["tags"]
    assert config["metadata"] == {"item_id": "email:em-001", "resume": False}
    assert "callbacks" not in config


def test_configure_langsmith_sets_env(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    configure_tracing(Settings(_env_file=None, tracing="langsmith", langsmith_project="proj"))
    assert os.environ["LANGSMITH_TRACING"] == "true" and os.environ["LANGSMITH_PROJECT"] == "proj"


def test_configure_none_leaves_env_alone(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    configure_tracing(Settings(_env_file=None, tracing="none"))
    assert "LANGSMITH_TRACING" not in os.environ


def test_traced_jev_ask_is_transparent_without_tracing(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    seen = {}

    def ask(payload, questions):
        seen.update(payload=payload, questions=questions)
        return "resposta"

    assert traced_jev_ask(ask, "jev-latest", {"x": 1}, {"q": "?"}) == "resposta"
    assert seen == {"payload": {"x": 1}, "questions": {"q": "?"}}
