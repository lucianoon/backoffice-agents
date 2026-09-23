"""eval-shadow com Jev que omite respostas: sem KeyError, ausência contada à parte e como erro.

Numa rodada ao vivo o emulador deixa de fora um Noul sem probabilidade válida (fail-closed). A
avaliação não pode quebrar nem inventar 0.5: a pergunta vira "sem resposta", pesa como erro na
acurácia e aparece na taxa de respostas ausentes do estágio.
"""

from pathlib import Path

from conftest import FakeJev

from backoffice_agents.eval_shadow import (
    _noul_metrics,
    load_dataset,
    load_jsonl,
    run_gate_shadow,
    run_shadow,
    run_verify_shadow,
    suggest_thresholds,
)
from backoffice_agents.jev.models import JevResponse

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data" / "samples"


class OmittingJev(FakeJev):
    """Oráculo que acerta tudo que responde, mas omite `omit` nas linhas `rows` (0-based)."""

    def __init__(self, dataset, omit: set[str], rows: set[int]) -> None:
        super().__init__()
        self.dataset, self.omit, self.rows = dataset, omit, rows

    def ask(self, state, questions):
        index = len(self.calls)
        labels = self.dataset[index]["labels"]
        self.overrides = {k: (0.9 if v else 0.1) for k, v in labels.items() if isinstance(v, bool)}
        if "category" in labels:
            self.overrides |= {"category": (labels["category"], 0.9), "urgency": float(labels["urgency"])}
        if "quality" in labels:
            self.overrides["quality"] = float(labels["quality"])
        response = super().ask(state, questions)
        if index not in self.rows:
            return response
        answers = {k: v for k, v in response.answers.items() if k not in self.omit}
        return JevResponse(model=response.model, answers=answers, latency_ms=1.0, calibrated=True)


def test_noul_metrics_count_missing_as_error_not_half():
    metrics = _noul_metrics([(0.9, True), (None, True), (0.1, False), (None, False)])
    assert metrics["accuracy"] == 0.5                 # 2 acertos em 4, ausentes são erro
    assert metrics["missing"] == 2 and metrics["missing_rate"] == 0.5
    assert abs(metrics["ece"] - 0.1) < 1e-9           # ECE só sobre as respondidas


def test_gate_shadow_missing_key_is_counted_not_key_error():
    dataset = load_jsonl(SAMPLES / "gate_labeled.jsonl")
    jev = OmittingJev(dataset, {"appropriate"}, rows={0, 1})
    result = run_gate_shadow(jev, dataset)

    n = len(dataset)
    assert result.metrics["appropriate"]["missing"] == 2
    assert result.metrics["appropriate"]["accuracy"] == (n - 2) / n
    assert result.metrics["args_complete"]["missing"] == 0
    assert result.metrics["args_complete"]["accuracy"] == 1.0
    assert result.missing_total == 2 and result.missing_rate == 2 / (2 * n)
    assert result.rows[0]["appropriate"] is None       # explícito, não 0.5


def test_verify_shadow_missing_noul_and_score():
    dataset = load_jsonl(SAMPLES / "verify_labeled.jsonl")
    jev = OmittingJev(dataset, {"resolves", "quality"}, rows={3})
    result = run_verify_shadow(jev, dataset)

    n = len(dataset)
    assert result.metrics["resolves"]["missing"] == 1
    assert result.metrics["resolves"]["accuracy"] == (n - 1) / n
    assert result.metrics["quality"]["missing"] == 1
    assert result.metrics["quality"]["within_one"] == (n - 1) / n
    assert result.missing_total == 2
    assert result.rows[3]["resolves"] is None and result.rows[3]["quality"] is None


def test_triage_shadow_missing_answers():
    dataset = load_dataset([str(SAMPLES / "emails.json"), str(SAMPLES / "emails_eval.json")],
                           str(SAMPLES / "labeled.jsonl"))
    assert len(dataset) >= 3
    jev = OmittingJev(dataset, {"needs_human", "category"}, rows={0})
    result = run_shadow(jev, dataset, "omite")

    n = result.n
    assert result.missing == {"category": 1, "needs_human": 1}
    assert result.missing_rate == 2 / (3 * n)
    assert result.category_accuracy == (n - 1) / n
    assert result.needs_human_accuracy == (n - 1) / n
    assert result.urgency_accuracy == 1.0
    assert len(result.category_bins) == n - 1          # ECE só sobre o respondido
    assert result.rows[0]["predicted"] is None and result.rows[0]["needs_human"] is None
    assert None not in suggest_thresholds(result.rows, min_support=1)


def test_report_prints_missing_rate_without_crashing(capsys):
    from backoffice_agents.cli import _print_stage

    dataset = load_jsonl(SAMPLES / "gate_labeled.jsonl")
    result = run_gate_shadow(OmittingJev(dataset, {"appropriate"}, rows={0}), dataset)
    _print_stage(result, "omite")
    out = capsys.readouterr().out
    assert f"respostas ausentes: 1/{2 * len(dataset)}" in out
    assert "sem resposta" in out
