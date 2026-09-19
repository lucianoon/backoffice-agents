import pytest

from backoffice_agents.jev.models import (
    ChoiceAnswer,
    ChoiceQuestion,
    NoulAnswer,
    ScoreAnswer,
    ScoreQuestion,
    parse_answer,
    questions_payload,
)


def test_parse_each_answer_type():
    assert parse_answer({"type": "noul", "noul": 0.87}) == NoulAnswer(noul=0.87)
    choice = parse_answer({"type": "choice", "choice": "a", "probabilities": {"a": 0.7, "b": 0.3},
                           "confidence": 0.7})
    assert isinstance(choice, ChoiceAnswer) and choice.choice == "a"
    score = parse_answer({"type": "score", "score": 2.4, "legend": ["x", "y", "z"],
                          "probabilities": {"1": 0.2, "2": 0.2, "3": 0.6}, "confidence": 0.6})
    assert isinstance(score, ScoreAnswer) and score.score == 2.4


def test_unknown_type_raises():
    with pytest.raises(ValueError):
        parse_answer({"type": "prose", "text": "não"})


def test_noul_confidence_is_distance_from_half():
    assert NoulAnswer(noul=0.5).confidence == 0
    assert NoulAnswer(noul=1.0).confidence == 1
    assert NoulAnswer(noul=0.0).confidence == 1


def test_choice_and_score_limits():
    with pytest.raises(ValueError):
        ChoiceQuestion(instructions="x", criteria={"only": None})
    with pytest.raises(ValueError):
        ScoreQuestion(instructions="x", criteria=["one"])


def test_payload_matches_api_shape():
    payload = questions_payload({
        "q": ChoiceQuestion(instructions="pick", criteria={"a": "A", "b": None}),
        "n": ScoreQuestion(instructions="rate", criteria=["low", "high"]),
    })
    assert payload["q"] == {"type": "choice", "instructions": "pick", "criteria": {"a": "A", "b": None}}
    assert payload["n"]["type"] == "score" and payload["n"]["criteria"] == ["low", "high"]
