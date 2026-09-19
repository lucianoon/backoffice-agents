from backoffice_agents.calibration import calibration_report
from backoffice_agents.eval_shadow import suggest_thresholds


def _dec(qid, kind, answer, label, model="jev"):
    return {"question_id": qid, "question_type": kind, "answer": answer, "human_label": label,
            "model": model}


def test_report_groups_by_question_and_model():
    decisions = [
        _dec("category", "choice", {"choice": "status_pedido", "confidence": 0.9}, "status_pedido"),
        _dec("category", "choice", {"choice": "outro", "confidence": 0.8}, "cancelamento"),
        _dec("category", "choice", {"choice": "outro", "confidence": 0.6}, "outro", model="emulado"),
        _dec("needs_human", "noul", {"noul": 0.9}, "true"),
        _dec("needs_human", "noul", {"noul": 0.2}, "sim"),          # errou
        _dec("urgency", "score", {"score": 2.4, "confidence": 0.5}, "3"),
        _dec("urgency", "score", {"score": 2.4, "confidence": 0.5}, None),   # sem rótulo: ignorado
        _dec("needs_human", "noul", {"noul": 0.7}, "talvez"),      # rótulo inválido: ignorado
    ]
    rows = {(r.question_id, r.model): r for r in calibration_report(decisions)}
    assert rows[("category", "jev")].n == 2 and rows[("category", "jev")].accuracy == 0.5
    assert rows[("category", "emulado")].n == 1 and rows[("category", "emulado")].accuracy == 1.0
    assert rows[("needs_human", "jev")].n == 2 and rows[("needs_human", "jev")].accuracy == 0.5
    assert rows[("urgency", "jev")].n == 1 and rows[("urgency", "jev")].accuracy == 1.0
    assert 0 <= rows[("category", "jev")].ece <= 1


def test_suggest_thresholds_picks_lowest_confidence_meeting_precision():
    rows = [
        {"predicted": "a", "expected": "a", "confidence": 0.95},
        {"predicted": "a", "expected": "a", "confidence": 0.90},
        {"predicted": "a", "expected": "a", "confidence": 0.80},
        {"predicted": "a", "expected": "b", "confidence": 0.70},   # erro abaixo de 0.80
        {"predicted": "b", "expected": "b", "confidence": 0.99},   # suporte insuficiente
        {"predicted": "b", "expected": "b", "confidence": 0.98},
    ]
    suggested = suggest_thresholds(rows, target_precision=0.95, min_support=3)
    assert suggested == {"a": 0.80, "b": None}
