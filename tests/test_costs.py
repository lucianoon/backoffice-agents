from backoffice_agents.config import Settings
from backoffice_agents.costs import cost_report

S = Settings(_env_file=None, llm_price_input_per_m=1.0, llm_price_output_per_m=2.0,
             jev_price_input_per_m=0.042)


def _call(item, kind, model, inp, out, ms, calibrated=True):
    return {"item_id": item, "kind": kind, "stage": "s", "model": model, "input_tokens": inp,
            "output_tokens": out, "latency_ms": ms, "calibrated": int(calibrated)}


def test_cost_report_groups_prices_and_what_if():
    calls = [
        _call("i1", "llm", "gpt", 1_000_000, 500_000, 2000),           # 1.0 + 1.0 = 2.0
        _call("i1", "jev", "jev-emulated", 1_000_000, 100_000, 1500, calibrated=False),  # 1.0 + 0.2
        _call("i2", "jev", "jev-1.13.0", 1_000_000, 0, 120),           # 0.042
        _call("i2", "jev", "jev-1.13.0", 1_000_000, 0, 80),            # 0.042
    ]
    report = cost_report(calls, S)
    by = {(u.kind, u.model): u for u in report.by_model}
    assert report.items == 2
    assert abs(by[("llm", "gpt")].cost_usd - 2.0) < 1e-9
    assert abs(by[("jev", "jev-emulated")].cost_usd - 1.2) < 1e-9
    assert abs(by[("jev", "jev-1.13.0")].cost_usd - 0.084) < 1e-9
    assert by[("jev", "jev-1.13.0")].calls == 2 and by[("jev", "jev-1.13.0")].mean_latency_ms == 100
    assert abs(report.llm_cost_usd - 2.0) < 1e-9 and abs(report.jev_cost_usd - 1.284) < 1e-9
    # "e se": o emulador gastou 1,2 com 1M tokens de entrada; no Jev real seriam 0,042
    assert report.emulated_input_tokens == 1_000_000
    assert abs(report.emulated_cost_usd - 1.2) < 1e-9
    assert abs(report.what_if_real_jev_usd - 0.042) < 1e-9
    assert abs(report.cost_per_item_usd - 3.284 / 2) < 1e-9


def test_empty_report():
    report = cost_report([], S)
    assert report.items == 0 and report.total_cost_usd == 0 and report.cost_per_item_usd == 0
