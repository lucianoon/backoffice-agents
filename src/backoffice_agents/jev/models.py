"""Primitivas do Jev (TypeSafe System One API): Noul, Choice e Score.

Formato de requisição/resposta segue docs.typesafe.ai/api. Os mesmos modelos são
usados pelo cliente real e pelo emulador, para que o restante do sistema não
saiba qual dos dois está ativo.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class NoulQuestion(BaseModel):
    type: Literal["noul"] = "noul"
    instructions: str
    criteria: dict[str, str] | None = None  # {"true": "...", "false": "..."}


class ChoiceQuestion(BaseModel):
    type: Literal["choice"] = "choice"
    instructions: str
    criteria: dict[str, str | None]  # opção -> descrição (max 255 opções)

    @model_validator(mode="after")
    def _check_options(self) -> "ChoiceQuestion":
        if not 2 <= len(self.criteria) <= 255:
            raise ValueError("choice precisa de 2 a 255 opções")
        return self


class ScoreQuestion(BaseModel):
    type: Literal["score"] = "score"
    instructions: str
    criteria: list[str]  # níveis ordenados (2 a 10)

    @model_validator(mode="after")
    def _check_levels(self) -> "ScoreQuestion":
        if not 2 <= len(self.criteria) <= 10:
            raise ValueError("score precisa de 2 a 10 níveis")
        return self


Question = NoulQuestion | ChoiceQuestion | ScoreQuestion


class NoulAnswer(BaseModel):
    type: Literal["noul"] = "noul"
    noul: float = Field(ge=0.0, le=1.0)

    @property
    def confidence(self) -> float:
        # Para noul a confiança está na própria probabilidade: distância de 0.5.
        return abs(self.noul - 0.5) * 2


class ChoiceAnswer(BaseModel):
    type: Literal["choice"] = "choice"
    choice: str
    probabilities: dict[str, float]
    confidence: float = Field(ge=0.0, le=1.0)


class ScoreAnswer(BaseModel):
    type: Literal["score"] = "score"
    score: float
    legend: list[str] = Field(default_factory=list)
    probabilities: dict[str, float] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)


Answer = NoulAnswer | ChoiceAnswer | ScoreAnswer


class JevResponse(BaseModel):
    model: str
    answers: dict[str, Answer]
    usage: dict[str, int] = Field(default_factory=dict)
    latency_ms: float = 0.0
    calibrated: bool = True  # False quando vem do emulador

    def noul(self, key: str) -> float:
        answer = self.answers[key]
        assert isinstance(answer, NoulAnswer), f"{key} não é noul"
        return answer.noul

    def choice(self, key: str) -> ChoiceAnswer:
        answer = self.answers[key]
        assert isinstance(answer, ChoiceAnswer), f"{key} não é choice"
        return answer

    def score(self, key: str) -> ScoreAnswer:
        answer = self.answers[key]
        assert isinstance(answer, ScoreAnswer), f"{key} não é score"
        return answer


def parse_answer(raw: dict[str, Any]) -> Answer:
    kind = raw.get("type")
    if kind == "noul":
        return NoulAnswer(noul=float(raw["noul"]))
    if kind == "choice":
        return ChoiceAnswer(
            choice=raw["choice"],
            probabilities={k: float(v) for k, v in raw.get("probabilities", {}).items()},
            confidence=float(raw.get("confidence", 0.0)),
        )
    if kind == "score":
        return ScoreAnswer(
            score=float(raw["score"]),
            legend=list(raw.get("legend", [])),
            probabilities={k: float(v) for k, v in raw.get("probabilities", {}).items()},
            confidence=float(raw.get("confidence", 0.0)),
        )
    raise ValueError(f"tipo de resposta desconhecido: {kind!r}")


def questions_payload(questions: dict[str, Question]) -> dict[str, Any]:
    return {key: q.model_dump(exclude_none=True) for key, q in questions.items()}
