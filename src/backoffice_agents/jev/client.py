"""Cliente HTTP do Jev real: POST https://api.typesafe.ai/v1/systemone."""

from __future__ import annotations

import time
from typing import Any, Protocol

import httpx

from .models import JevResponse, Question, parse_answer, questions_payload

RETRY_STATUSES = {429, 529}


class JevClient(Protocol):
    calibrated: bool

    def ask(self, state: str | dict[str, Any] | list[Any], questions: dict[str, Question]) -> JevResponse: ...


class RealJevClient:
    calibrated = True

    def __init__(self, api_key: str, model: str = "jev-latest",
                 base_url: str = "https://api.typesafe.ai/v1/systemone",
                 timeout_s: float = 15.0, max_retries: int = 3) -> None:
        if not api_key:
            raise ValueError("TYPESAFE_API_KEY é obrigatória com JEV_MODE=real")
        self._model = model
        self._url = base_url
        self._max_retries = max_retries
        self._http = httpx.Client(
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )

    def ask(self, state: str | dict[str, Any] | list[Any], questions: dict[str, Question]) -> JevResponse:
        body = {"model": self._model, "state": state, "questions": questions_payload(questions)}
        started = time.perf_counter()
        delay = 0.5
        for attempt in range(self._max_retries + 1):
            response = self._http.post(self._url, json=body)
            if response.status_code in RETRY_STATUSES and attempt < self._max_retries:
                time.sleep(delay)
                delay *= 2
                continue
            response.raise_for_status()
            data = response.json()
            return JevResponse(
                model=data.get("model", self._model),
                answers={k: parse_answer(v) for k, v in data["answers"].items()},
                usage=data.get("usage", {}),
                latency_ms=(time.perf_counter() - started) * 1000,
                calibrated=True,
            )
        raise RuntimeError("unreachable")
