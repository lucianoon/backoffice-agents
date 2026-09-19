from conftest import ingest_only
from langchain_core.messages import AIMessage

from backoffice_agents.config import Settings
from backoffice_agents.costs import cost_report
from backoffice_agents.ratelimit import RateLimiter


def test_rate_limiter_waits_when_window_is_full():
    clock = {"t": 0.0}
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    limiter = RateLimiter(per_minute=3, clock=lambda: clock["t"], sleep=sleep)
    assert [limiter.acquire() for _ in range(3)] == [0.0, 0.0, 0.0]
    waited = limiter.acquire()                    # 4a chamada: espera a janela de 60 s
    assert waited == 60.0 and sleeps == [60.0]
    assert RateLimiter(per_minute=0).acquire() == 0.0   # desligado


def test_worker_processes_items_in_parallel(make_runner, settings):
    settings.worker_concurrency = 3
    runner = make_runner([AIMessage(content="ok\nEquipe de Atendimento")] * 7)
    runner.ingest_emails()
    outcomes = runner.run_pending()
    assert len(outcomes) == 7
    statuses = {s for _, s in outcomes}
    assert statuses <= {"sent", "discarded", "escalated", "awaiting_approval"}
    assert runner.run_pending() == []
    assert all(i["status"] not in {"new", "processing"} for i in runner.store.list_items())


def test_emulator_pricing_is_used_for_uncalibrated_jev_calls():
    s = Settings(_env_file=None, llm_price_input_per_m=1.0, llm_price_output_per_m=2.0,
                 emulator_price_input_per_m=0.1, emulator_price_output_per_m=0.4)
    calls = [{"item_id": "i", "kind": "jev", "stage": "s", "model": "jev-emulated", "input_tokens": 1_000_000,
              "output_tokens": 1_000_000, "latency_ms": 1, "calibrated": 0}]
    report = cost_report(calls, s)
    assert abs(report.jev_cost_usd - 0.5) < 1e-9                   # 0.1 + 0.4, e não 1.0 + 2.0


def test_emulator_uses_cheaper_model_when_configured(monkeypatch, settings):
    from backoffice_agents import llm as llm_module

    built = []
    monkeypatch.setattr(llm_module, "build_llm",
                        lambda s, model=None: built.append(model or s.llm_model) or object())
    settings.emulator_model = "gpt-4.1-nano"
    llm_module.build_emulator_llm(settings)
    settings.emulator_model = None
    same = object()
    assert llm_module.build_emulator_llm(settings, same) is same
    assert built == ["gpt-4.1-nano"]


def test_default_has_single_llm(make_runner):
    runner = make_runner([AIMessage(content="ok\nEquipe de Atendimento")])
    ingest_only(runner, "email:em-001")
    assert runner.process_item("email:em-001")["status"] == "sent"
