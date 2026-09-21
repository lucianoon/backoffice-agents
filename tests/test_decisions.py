from backoffice_agents.decisions import gate_questions, triage_questions, verify_questions


def test_gate_questions_separate_kind_from_arguments():
    qs = gate_questions("crm_log_interaction")
    assert "right *kind*" in qs["appropriate"].instructions
    assert "Ignore whether the arguments" in qs["appropriate"].instructions
    assert "usable as-is" in qs["args_complete"].instructions
    assert "PED-" in qs["args_complete"].criteria["true"]
    assert "invents an outcome" in qs["args_complete"].criteria["false"]
    assert "spam" in qs["appropriate"].criteria["false"]


def test_verify_questions_name_what_counts_as_unsupported():
    qs = verify_questions()
    assert "discount" in qs["unsupported_claims"].criteria["true"]
    assert "KB passage" in qs["unsupported_claims"].criteria["false"]
    assert "thanks the customer" in qs["resolves"].criteria["false"]


def test_needs_human_does_not_treat_impatience_as_escalation():
    qs = triage_questions()
    assert "impatient" in qs["needs_human"].criteria["false"]
    assert "legal threat" in qs["needs_human"].criteria["true"]
