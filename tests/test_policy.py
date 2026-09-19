from backoffice_agents.config import Settings
from backoffice_agents.policy import GateOutcome, RiskLevel, Tier, gate_outcome, tier_for

S = Settings(_env_file=None)


def test_tiers():
    assert tier_for(0.95, S) == Tier.AUTO
    assert tier_for(0.85, S) == Tier.AUTO
    assert tier_for(0.7, S) == Tier.REVIEW
    assert tier_for(0.2, S) == Tier.ESCALATE


def test_per_category_thresholds_override_globals():
    s = Settings(_env_file=None, confidence_auto_by_category={"cancelamento": 0.97},
                 confidence_review_by_category={"spam_irrelevante": 0.3})
    assert tier_for(0.9, s, "cancelamento") == Tier.REVIEW     # global diria AUTO
    assert tier_for(0.9, s, "status_pedido") == Tier.AUTO      # sem override
    assert tier_for(0.4, s, "spam_irrelevante") == Tier.REVIEW  # global diria ESCALATE
    assert tier_for(0.9, s) == Tier.AUTO


def test_high_and_critical_always_need_human():
    for risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
        outcome, _ = gate_outcome(risk, 0.99, 0.99, S)
        assert outcome == GateOutcome.APPROVE


def test_low_executes_without_gate():
    assert gate_outcome(RiskLevel.LOW, None, None, S)[0] == GateOutcome.EXECUTE


def test_medium_depends_on_gate():
    assert gate_outcome(RiskLevel.MEDIUM, 0.9, 0.9, S)[0] == GateOutcome.EXECUTE
    assert gate_outcome(RiskLevel.MEDIUM, 0.6, 0.9, S)[0] == GateOutcome.APPROVE   # abaixo de 0.70
    assert gate_outcome(RiskLevel.MEDIUM, 0.3, 0.9, S)[0] == GateOutcome.REJECT    # inadequada
    assert gate_outcome(RiskLevel.MEDIUM, 0.9, 0.2, S)[0] == GateOutcome.REJECT    # args ruins
    assert gate_outcome(RiskLevel.MEDIUM, None, None, S)[0] == GateOutcome.APPROVE  # gate caiu
