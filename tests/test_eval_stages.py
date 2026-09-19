from pathlib import Path

from conftest import FakeJev

from backoffice_agents.eval_shadow import load_jsonl, run_gate_shadow, run_verify_shadow

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"


def test_datasets_load():
    gate = load_jsonl(SAMPLES / "gate_labeled.jsonl")
    verify = load_jsonl(SAMPLES / "verify_labeled.jsonl")
    assert len(gate) >= 10 and all({"tool", "args", "labels"} <= set(r) for r in gate)
    assert len(verify) >= 10 and all({"draft", "facts", "labels"} <= set(r) for r in verify)
    assert any(not r["labels"]["appropriate"] for r in gate)          # tem negativos
    assert any(r["labels"]["unsupported_claims"] for r in verify)


def test_gate_shadow_scores_each_noul():
    dataset = load_jsonl(SAMPLES / "gate_labeled.jsonl")
    # um Jev que "sabe" a resposta: usa os rótulos como oráculo com confiança 0.9
    answers = iter(dataset)

    class Oracle(FakeJev):
        def ask(self, state, questions):
            row = next(answers)
            self.overrides = {"appropriate": 0.9 if row["labels"]["appropriate"] else 0.1,
                              "args_complete": 0.9 if row["labels"]["args_complete"] else 0.1}
            return super().ask(state, questions)

    result = run_gate_shadow(Oracle(), dataset)
    assert result.stage == "gate" and result.n == len(dataset)
    assert result.metrics["appropriate"]["accuracy"] == 1.0
    assert result.metrics["args_complete"]["accuracy"] == 1.0
    assert abs(result.metrics["appropriate"]["ece"] - 0.1) < 1e-9     # conf 0.9, acerto 100%


def test_verify_shadow_scores_nouls_and_quality():
    dataset = load_jsonl(SAMPLES / "verify_labeled.jsonl")
    jev = FakeJev({"resolves": 0.9, "unsupported_claims": 0.1, "quality": 4.0})   # sempre "aprova"
    result = run_verify_shadow(jev, dataset)
    positives = sum(1 for r in dataset if r["labels"]["resolves"])
    assert result.metrics["resolves"]["accuracy"] == positives / len(dataset)
    assert 0 < result.metrics["unsupported_claims"]["accuracy"] < 1
    assert 0 <= result.metrics["quality"]["within_one"] <= 1
    assert result.rows[0]["id"] == "v-01"
