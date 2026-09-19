import json

import pytest
from conftest import ScriptedLLM
from langchain_core.messages import AIMessage, HumanMessage

from backoffice_agents.jev.emulated import EmulatedJevClient
from backoffice_agents.jev.models import NoulQuestion
from backoffice_agents.replay import ReplayChatModel, ReplayMiss


def test_record_then_replay_is_deterministic_and_offline(tmp_path):
    cassette = tmp_path / "c.json"
    inner = ScriptedLLM(script=[AIMessage(content='{"q": {"p_true": 0.8}}')])
    recorder = ReplayChatModel(cassette_path=str(cassette), mode="record", inner=inner)
    first = EmulatedJevClient(recorder).ask("estado", {"q": NoulQuestion(instructions="?")})
    assert first.noul("q") == 0.8 and recorder.misses == 1
    assert len(json.loads(cassette.read_text(encoding="utf-8"))) == 1

    player = ReplayChatModel(cassette_path=str(cassette), mode="replay")   # sem LLM interno
    second = EmulatedJevClient(player).ask("estado", {"q": NoulQuestion(instructions="?")})
    assert second.noul("q") == 0.8 and player.hits == 1 and inner.calls == 1


def test_replay_miss_is_explicit(tmp_path):
    player = ReplayChatModel(cassette_path=str(tmp_path / "vazio.json"), mode="replay")
    with pytest.raises(ReplayMiss, match="--record"):
        player.invoke([HumanMessage(content="nunca gravado")])


def test_record_requires_inner():
    with pytest.raises(ValueError):
        ReplayChatModel(cassette_path="x.json", mode="record")
