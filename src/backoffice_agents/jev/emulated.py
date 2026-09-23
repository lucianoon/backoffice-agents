"""Emulador do Jev com um LLM comum.

Serve para desenvolver e testar o fluxo enquanto a chave do Jev não chega.
As probabilidades NÃO são calibradas: `calibrated=False` em toda resposta,
e o log de decisões guarda isso para não contaminar a avaliação.
"""

from __future__ import annotations

import json
import math
import re
import time
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from .models import (
    Answer,
    ChoiceAnswer,
    ChoiceQuestion,
    JevResponse,
    NoulAnswer,
    NoulQuestion,
    Question,
    ScoreAnswer,
    ScoreQuestion,
    questions_payload,
)

SYSTEM_PROMPT = """You emulate a calibrated decision model. You never generate prose.
Given a STATE and a set of typed QUESTIONS, answer every question with probabilities.
Rules:
- noul: return {"p_true": number in [0,1]}.
- choice: return {"probabilities": {option: number}} covering ALL options, summing to 1.
- score: return {"probabilities": {level_index_starting_at_1: number}} covering ALL levels, summing to 1.
Be honest about uncertainty: spread probability when the state is ambiguous.
Return ONLY a JSON object keyed by question id. No markdown, no commentary."""


class EmulatedJevClient:
    calibrated = False

    def __init__(self, llm: BaseChatModel, model_name: str = "jev-emulated") -> None:
        self._llm = llm
        self._model = model_name

    def ask(self, state: str | dict[str, Any] | list[Any], questions: dict[str, Question]) -> JevResponse:
        started = time.perf_counter()
        state_text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False, indent=2)
        questions_text = json.dumps(questions_payload(questions), ensure_ascii=False, indent=2)
        prompt = f"STATE:\n{state_text}\n\nQUESTIONS:\n{questions_text}"
        raw = self._llm.invoke([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)])
        data = _extract_json(raw.content if isinstance(raw.content, str) else str(raw.content))
        answers: dict[str, Answer] = {}
        for key, question in questions.items():
            answer = _to_answer(question, data.get(key, {}))
            if answer is not None:  # noul sem probabilidade válida fica de fora (fail-closed)
                answers[key] = answer
        return JevResponse(
            model=self._model,
            answers=answers,
            usage=_usage(raw),
            latency_ms=(time.perf_counter() - started) * 1000,
            calibrated=False,
        )


def _usage(message: Any) -> dict[str, int]:
    meta = getattr(message, "usage_metadata", None) or {}
    return {"input_tokens": int(meta.get("input_tokens", 0)),
            "output_tokens": int(meta.get("output_tokens", 0))}


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise ValueError(f"emulador não devolveu JSON: {text[:200]!r}") from None
        return json.loads(match.group(0))


def _normalize(probs: dict[str, float], keys: list[str]) -> dict[str, float]:
    values = {k: max(0.0, float(probs.get(k, 0.0))) for k in keys}
    total = sum(values.values())
    if total <= 0:
        return {k: 1.0 / len(keys) for k in keys}
    return {k: v / total for k, v in values.items()}


def _noul_value(raw: Any) -> float | None:
    """p_true do emulador; None se ausente ou não numérico (não inventa 0.5)."""
    value = raw.get("p_true", raw.get("noul")) if isinstance(raw, dict) else None
    if value is None or isinstance(value, bool):
        return None
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    return min(1.0, max(0.0, p)) if math.isfinite(p) else None


def _to_answer(question: Question, raw: dict[str, Any]) -> Answer | None:
    """Converte a resposta do LLM.

    Noul ausente ou inválido devolve None e fica fora da resposta: a triagem e a verificação escalam
    e o gate de ferramenta pede aprovação humana, em vez de receberem um 0.5 inventado.
    """
    if isinstance(question, NoulQuestion):
        p = _noul_value(raw)
        return None if p is None else NoulAnswer(noul=p)
    if isinstance(question, ChoiceQuestion):
        options = list(question.criteria.keys())
        probs = _normalize(raw.get("probabilities", {}), options)
        best = max(probs, key=lambda option: probs[option])
        return ChoiceAnswer(choice=best, probabilities=probs, confidence=probs[best])
    if isinstance(question, ScoreQuestion):
        levels = [str(i) for i in range(1, len(question.criteria) + 1)]
        probs = _normalize({str(k): v for k, v in raw.get("probabilities", {}).items()}, levels)
        score = sum(int(level) * p for level, p in probs.items())
        return ScoreAnswer(score=score, legend=list(question.criteria), probabilities=probs,
                           confidence=max(probs.values()))
    raise TypeError(type(question))
