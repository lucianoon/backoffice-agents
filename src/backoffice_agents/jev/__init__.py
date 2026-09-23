from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from ..config import Settings
from .client import (
    JevClient,
    JevError,
    JevHTTPError,
    JevMalformedResponseError,
    JevUnavailableError,
    RealJevClient,
)
from .emulated import EmulatedJevClient
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
)

__all__ = [
    "Answer", "ChoiceAnswer", "ChoiceQuestion", "JevClient", "JevError", "JevHTTPError",
    "JevMalformedResponseError", "JevResponse", "JevUnavailableError", "NoulAnswer",
    "NoulQuestion", "Question", "ScoreAnswer", "ScoreQuestion", "build_jev_client",
]


def build_jev_client(settings: Settings, llm: BaseChatModel) -> JevClient:
    if settings.jev_mode == "real":
        return RealJevClient.from_settings(settings)
    return EmulatedJevClient(llm)
