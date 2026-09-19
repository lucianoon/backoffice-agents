from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from backoffice_agents.adapters import build_adapters
from backoffice_agents.config import Settings
from backoffice_agents.jev.models import (
    ChoiceAnswer,
    ChoiceQuestion,
    JevResponse,
    NoulAnswer,
    NoulQuestion,
    Question,
    ScoreAnswer,
    ScoreQuestion,
)
from backoffice_agents.runner import Runner
from backoffice_agents.storage import Store

ROOT = Path(__file__).resolve().parents[1]


class ScriptedLLM(BaseChatModel):
    """Devolve as mensagens do roteiro em ordem. bind_tools é no-op."""

    script: list[AIMessage]
    calls: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        if self.calls >= len(self.script):
            raise AssertionError(f"LLM chamado {self.calls + 1}x, roteiro tem {len(self.script)}")
        message = self.script[self.calls]
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self


def tool_call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(content="",
                     tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


DEFAULTS: dict[str, Any] = {
    "category": ("status_pedido", 0.92),
    "urgency": 2.0,
    "needs_human": 0.1,
    "sensitive": 0.05,
    "injection": 0.05,
    "appropriate": 0.9,
    "args_complete": 0.9,
    "resolves": 0.9,
    "quality": 4.2,
    "unsupported_claims": 0.1,
}


class FakeJev:
    """Responde por chave da pergunta. `overrides` pode ser valor fixo ou função(chamada_n)."""

    calibrated = True

    def __init__(self, overrides: dict[str, Any] | None = None, fail_first: int = 0) -> None:
        self.overrides = overrides or {}
        self.calls: list[dict[str, Question]] = []
        self.states: list[Any] = []
        self.counts: dict[str, int] = {}
        # chamadas (1-based) que levantam erro, simulando a API fora do ar
        self.fail_calls: set[int] = set(range(1, fail_first + 1))

    def _value(self, key: str, question: Question) -> Any:
        n = self.counts.get(key, 0)
        self.counts[key] = n + 1
        if key in self.overrides:
            value = self.overrides[key]
        elif key in DEFAULTS:
            value = DEFAULTS[key]
        elif isinstance(question, ScoreQuestion):   # ex.: relevância p0..pN da base de conhecimento
            value = self.overrides.get("_score_default", 3.0)
        elif isinstance(question, NoulQuestion):
            value = 0.5
        else:
            value = (next(iter(question.criteria)), 0.5)
        return value(n) if isinstance(value, Callable) else value

    def ask(self, state, questions: dict[str, Question]) -> JevResponse:
        self.calls.append(questions)
        self.states.append(state)
        if len(self.calls) in self.fail_calls:
            raise ConnectionError("jev fora do ar")
        answers = {}
        for key, question in questions.items():
            value = self._value(key, question)
            if isinstance(question, NoulQuestion):
                answers[key] = NoulAnswer(noul=float(value))
            elif isinstance(question, ChoiceQuestion):
                choice, confidence = value
                rest = (1 - confidence) / (len(question.criteria) - 1)
                probs = {opt: (confidence if opt == choice else rest) for opt in question.criteria}
                answers[key] = ChoiceAnswer(choice=choice, probabilities=probs, confidence=confidence)
            elif isinstance(question, ScoreQuestion):
                answers[key] = ScoreAnswer(score=float(value), legend=list(question.criteria),
                                           probabilities={}, confidence=0.8)
        return JevResponse(model="fake-jev", answers=answers, latency_ms=5.0, calibrated=True)


def ingest_only(runner: Runner, item_id: str) -> None:
    """Ingere os e-mails de exemplo mas deixa só `item_id` como novo (os outros viram 'skipped')."""
    runner.ingest_emails()
    for item in runner.store.list_items("new"):
        if item["id"] != item_id:
            runner.store.set_item_state(item["id"], "skipped", {})


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, db_url="sqlite:///" + (tmp_path / "test.db").as_posix(),
                    samples_path=str(ROOT / "data" / "samples" / "emails.json"),
                    telegram_mock_auto_approve=True, retry_delay_s=0)


@pytest.fixture
def make_runner(settings: Settings):
    def _make(script: list[AIMessage], jev_overrides: dict[str, Any] | None = None,
              jev: FakeJev | None = None, jev_fallback: FakeJev | None = None) -> Runner:
        return Runner(settings=settings, llm=ScriptedLLM(script=script), jev=jev or FakeJev(jev_overrides),
                      adapters=build_adapters(settings), store=Store(settings.db_url),
                      jev_fallback=jev_fallback)
    return _make
