"""Cliente HTTP do Jev real: POST https://api.typesafe.ai/v1/systemone.

Falhas viram erros tipados (`JevError` e subclasses), nunca `KeyError` ou exceções cruas do httpx:
quem chama (`Nodes._ask`) trata qualquer erro como Jev indisponível e segue o caminho fail-closed
(fallback emulado, se configurado; senão, humano).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Protocol

import httpx
from pydantic import ValidationError

from ..ratelimit import RateLimiter
from .models import Answer, JevResponse, Question, parse_answer, questions_payload

if TYPE_CHECKING:
    from ..config import Settings


class JevClient(Protocol):
    calibrated: bool

    def ask(self, state: str | dict[str, Any] | list[Any], questions: dict[str, Question]) -> JevResponse: ...


class JevError(RuntimeError):
    """Base dos erros do cliente real do Jev."""


class JevUnavailableError(JevError):
    """Transporte/timeout, ou 429/5xx depois de esgotar as tentativas."""

    def __init__(self, message: str, status_code: int | None = None, attempts: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts


class JevHTTPError(JevError):
    """Status HTTP sem retry (4xx exceto 429): chave inválida, requisição rejeitada."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class JevMalformedResponseError(JevError):
    """Resposta 2xx fora do contrato: JSON inválido, sem `answers`, resposta ilegível ou faltando."""


def is_retryable_status(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def retry_after_seconds(value: str | None,
                        now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> float | None:
    """Retry-After em segundos (RFC 9110: segundos ou data HTTP). None se ausente ou ilegível."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now()).total_seconds())


class RealJevClient:
    calibrated = True

    def __init__(self, api_key: str, model: str = "jev-latest",
                 base_url: str = "https://api.typesafe.ai/v1/systemone",
                 timeout_s: float = 15.0, max_retries: int = 3, max_rpm: int = 0, *,
                 backoff_s: float = 0.5, backoff_max_s: float = 10.0,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not api_key:
            raise ValueError("TYPESAFE_API_KEY é obrigatória com JEV_MODE=real")
        self._model = model
        self._url = base_url
        self._max_retries = max(0, max_retries)
        self._backoff_s = backoff_s
        self._backoff_max_s = backoff_max_s
        self._sleep = sleep
        self._limiter = RateLimiter(max_rpm)  # compartilhado entre as threads do worker
        self._http = httpx.Client(
            timeout=httpx.Timeout(timeout_s),
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> RealJevClient:
        return cls(api_key=settings.typesafe_api_key or "", model=settings.jev_model,
                   base_url=settings.jev_base_url, timeout_s=settings.jev_timeout_s,
                   max_retries=settings.jev_max_retries, max_rpm=settings.jev_max_rpm,
                   backoff_s=settings.jev_backoff_s, backoff_max_s=settings.jev_backoff_max_s, **kwargs)

    def _retry_wait(self, attempt: int, response: httpx.Response | None) -> float | None:
        """Espera antes da próxima tentativa; None quando não vale tentar de novo.

        Backoff exponencial limitado a `backoff_max_s`. Um Retry-After do servidor vence o backoff;
        se ele pedir mais que `backoff_max_s`, desiste já em vez de prender o worker.
        """
        if attempt >= self._max_retries:
            return None
        backoff = min(self._backoff_s * (2 ** attempt), self._backoff_max_s)
        hinted = retry_after_seconds(response.headers.get("Retry-After")) if response is not None else None
        if hinted is None:
            return backoff
        return hinted if hinted <= self._backoff_max_s else None

    def ask(self, state: str | dict[str, Any] | list[Any], questions: dict[str, Question]) -> JevResponse:
        body = {"model": self._model, "state": state, "questions": questions_payload(questions)}
        started = time.perf_counter()
        attempt = 0
        while True:
            self._limiter.acquire()
            try:
                response = self._http.post(self._url, json=body)
            except httpx.TransportError as exc:  # inclui timeouts (httpx.TimeoutException)
                wait = self._retry_wait(attempt, None)
                if wait is None:
                    raise JevUnavailableError(
                        f"Jev inacessível após {attempt + 1} tentativa(s): {exc.__class__.__name__}",
                        attempts=attempt + 1) from exc
            else:
                status = response.status_code
                if not is_retryable_status(status):
                    if status >= 400:
                        raise JevHTTPError(f"Jev rejeitou a requisição: HTTP {status}", status)
                    return self._parse(response, questions, started)
                wait = self._retry_wait(attempt, response)
                if wait is None:
                    raise JevUnavailableError(
                        f"Jev respondeu HTTP {status} após {attempt + 1} tentativa(s)",
                        status_code=status, attempts=attempt + 1)
            self._sleep(wait)
            attempt += 1

    def _parse(self, response: httpx.Response, questions: dict[str, Question],
               started: float) -> JevResponse:
        try:
            data = response.json()
        except ValueError as exc:
            raise JevMalformedResponseError("resposta do Jev não é JSON") from exc
        if not isinstance(data, dict):
            raise JevMalformedResponseError("resposta do Jev não é um objeto JSON")
        raw_answers = data.get("answers")
        if not isinstance(raw_answers, dict):
            raise JevMalformedResponseError("resposta do Jev sem o campo 'answers'")
        answers: dict[str, Answer] = {}
        for key, raw in raw_answers.items():
            if not isinstance(raw, dict):
                raise JevMalformedResponseError(f"resposta do Jev para {key!r} não é um objeto")
            try:
                answers[key] = parse_answer(raw)
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                raise JevMalformedResponseError(
                    f"resposta do Jev para {key!r} ilegível ({exc.__class__.__name__})") from exc
        missing = sorted(set(questions) - set(answers))
        if missing:
            raise JevMalformedResponseError(f"resposta do Jev sem as perguntas: {', '.join(missing)}")
        usage = data.get("usage")
        try:
            return JevResponse(
                model=str(data.get("model") or self._model),
                answers=answers,
                usage=usage if isinstance(usage, dict) else {},
                latency_ms=(time.perf_counter() - started) * 1000,
                calibrated=True,
            )
        except ValidationError as exc:
            raise JevMalformedResponseError(f"resposta do Jev fora do contrato: {exc.error_count()} erro(s)"
                                            ) from exc
