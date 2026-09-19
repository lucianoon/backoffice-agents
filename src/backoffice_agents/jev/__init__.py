from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from ..config import Settings
from .client import JevClient, RealJevClient
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
    "Answer", "ChoiceAnswer", "ChoiceQuestion", "JevClient", "JevResponse", "NoulAnswer",
    "NoulQuestion", "Question", "ScoreAnswer", "ScoreQuestion", "build_jev_client",
]


def build_jev_client(settings: Settings, llm: BaseChatModel) -> JevClient:
    if settings.jev_mode == "real":
        return RealJevClient(
            api_key=settings.typesafe_api_key or "",
            model=settings.jev_model,
            base_url=settings.jev_base_url,
            timeout_s=settings.jev_timeout_s,
            max_rpm=settings.jev_max_rpm,
        )
    return EmulatedJevClient(llm)
