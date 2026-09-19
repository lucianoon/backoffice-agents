import json

from langchain_core.messages import AIMessage

from backoffice_agents.jev.emulated import EmulatedJevClient
from backoffice_agents.jev.models import ChoiceQuestion, NoulQuestion, ScoreQuestion
from conftest import ScriptedLLM


def test_emulator_normalizes_and_flags_uncalibrated():
    raw = {
        "yes": {"p_true": 0.8},
        "pick": {"probabilities": {"a": 2, "b": 1}},          # soma 3 -> normaliza
        "rate": {"probabilities": {"1": 0.0, "2": 0.5, "3": 0.5}},
    }
    llm = ScriptedLLM(script=[AIMessage(content="```json\n" + json.dumps(raw) + "\n```")])
    client = EmulatedJevClient(llm)
    response = client.ask("estado", {
        "yes": NoulQuestion(instructions="?"),
        "pick": ChoiceQuestion(instructions="?", criteria={"a": None, "b": None}),
        "rate": ScoreQuestion(instructions="?", criteria=["l1", "l2", "l3"]),
    })
    assert response.calibrated is False
    assert response.noul("yes") == 0.8
    pick = response.choice("pick")
    assert pick.choice == "a" and abs(pick.probabilities["a"] - 2 / 3) < 1e-9
    assert abs(response.score("rate").score - 2.5) < 1e-9


def test_emulator_missing_option_gets_uniform():
    llm = ScriptedLLM(script=[AIMessage(content='{"pick": {"probabilities": {}}}')])
    response = EmulatedJevClient(llm).ask("x", {
        "pick": ChoiceQuestion(instructions="?", criteria={"a": None, "b": None})})
    assert response.choice("pick").probabilities == {"a": 0.5, "b": 0.5}
