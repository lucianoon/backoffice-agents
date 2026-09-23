"""Cliente HTTP do Jev real com httpx.MockTransport: nada sai para a rede.

Timeout, retry com backoff limitado em 429/5xx/erro de transporte (respeitando Retry-After) e erro
tipado para payload fora do contrato. Os últimos testes ligam o cliente ao fluxo: qualquer erro dele
leva o item ao caminho fail-closed (humano), sem chamar o LLM e sem derrubar o worker.
"""

import json
from datetime import UTC, datetime

import httpx
import pytest
from conftest import FakeJev, ingest_only

from backoffice_agents.config import Settings
from backoffice_agents.jev import build_jev_client
from backoffice_agents.jev.client import (
    JevError,
    JevHTTPError,
    JevMalformedResponseError,
    JevUnavailableError,
    RealJevClient,
    retry_after_seconds,
)
from backoffice_agents.jev.emulated import EmulatedJevClient
from backoffice_agents.jev.models import NoulQuestion

URL = "https://jev.test/v1/systemone"
QUESTIONS = {"ok": NoulQuestion(instructions="ok?")}
GOOD = {"model": "jev-1", "answers": {"ok": {"type": "noul", "noul": 0.8}},
        "usage": {"input_tokens": 12, "output_tokens": 0}}
MARIANA = "email:em-001"


class Recorder:
    """Transporte roteirizado: cada item é um httpx.Response, uma exceção ou um dict (JSON 200)."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, Exception):
            raise step
        if isinstance(step, httpx.Response):
            return step
        return httpx.Response(200, json=step)


def _client(recorder, sleeps: list[float] | None = None, **kwargs) -> RealJevClient:
    sleeps = sleeps if sleeps is not None else []
    return RealJevClient("k", base_url=URL, transport=httpx.MockTransport(recorder),
                         sleep=sleeps.append, **kwargs)


def test_success_parses_answers_and_sends_contract():
    recorder = Recorder(GOOD)
    response = _client(recorder, model="jev-latest").ask({"x": 1}, QUESTIONS)

    assert response.noul("ok") == 0.8
    assert response.model == "jev-1" and response.calibrated is True
    assert response.usage == {"input_tokens": 12, "output_tokens": 0}
    request = recorder.requests[0]
    assert request.headers["Authorization"] == "Bearer k"
    body = json.loads(request.content)
    assert body["model"] == "jev-latest" and body["state"] == {"x": 1}
    assert body["questions"]["ok"]["type"] == "noul"


def test_429_then_success_respects_retry_after():
    recorder = Recorder(httpx.Response(429, headers={"Retry-After": "2"}), GOOD)
    sleeps: list[float] = []
    response = _client(recorder, sleeps).ask("s", QUESTIONS)

    assert response.noul("ok") == 0.8
    assert len(recorder.requests) == 2
    assert sleeps == [2.0]                       # o Retry-After vence o backoff padrão (0.5)


def test_429_without_retry_after_uses_exponential_backoff():
    recorder = Recorder(httpx.Response(429), httpx.Response(429), GOOD)
    sleeps: list[float] = []
    _client(recorder, sleeps, backoff_s=0.5).ask("s", QUESTIONS)
    assert sleeps == [0.5, 1.0]


def test_5xx_exhausts_retries_with_capped_backoff():
    recorder = Recorder(httpx.Response(503))
    sleeps: list[float] = []
    client = _client(recorder, sleeps, max_retries=4, backoff_s=1.0, backoff_max_s=3.0)
    with pytest.raises(JevUnavailableError) as info:
        client.ask("s", QUESTIONS)

    assert info.value.status_code == 503 and info.value.attempts == 5
    assert len(recorder.requests) == 5           # 1 + max_retries
    assert sleeps == [1.0, 2.0, 3.0, 3.0]        # exponencial, limitado a backoff_max_s


def test_retry_after_beyond_cap_gives_up_immediately():
    recorder = Recorder(httpx.Response(429, headers={"Retry-After": "120"}), GOOD)
    sleeps: list[float] = []
    with pytest.raises(JevUnavailableError) as info:
        _client(recorder, sleeps, backoff_max_s=10.0).ask("s", QUESTIONS)
    assert info.value.status_code == 429
    assert len(recorder.requests) == 1 and sleeps == []


def test_transport_error_is_retried_then_typed():
    recorder = Recorder(httpx.ConnectError("recusada"))
    sleeps: list[float] = []
    with pytest.raises(JevUnavailableError) as info:
        _client(recorder, sleeps, max_retries=2).ask("s", QUESTIONS)

    assert info.value.status_code is None and info.value.attempts == 3
    assert isinstance(info.value.__cause__, httpx.ConnectError)
    assert len(recorder.requests) == 3 and len(sleeps) == 2


def test_timeout_then_success():
    recorder = Recorder(httpx.ReadTimeout("lento"), GOOD)
    assert _client(recorder).ask("s", QUESTIONS).noul("ok") == 0.8
    assert len(recorder.requests) == 2


def test_timeout_is_configurable():
    client = RealJevClient("k", timeout_s=2.5)
    assert client._http.timeout == httpx.Timeout(2.5)
    settings = Settings(_env_file=None, jev_mode="real", typesafe_api_key="k", jev_timeout_s=4.0,
                        jev_max_retries=5, jev_backoff_s=0.1, jev_backoff_max_s=2.0)
    built = build_jev_client(settings, llm=None)  # type: ignore[arg-type]
    assert isinstance(built, RealJevClient)
    assert built._http.timeout == httpx.Timeout(4.0)
    assert (built._max_retries, built._backoff_s, built._backoff_max_s) == (5, 0.1, 2.0)


def test_non_retryable_4xx_is_typed_without_retry():
    recorder = Recorder(httpx.Response(401, json={"error": "chave inválida"}))
    sleeps: list[float] = []
    with pytest.raises(JevHTTPError) as info:
        _client(recorder, sleeps).ask("s", QUESTIONS)
    assert info.value.status_code == 401
    assert len(recorder.requests) == 1 and sleeps == []


@pytest.mark.parametrize("payload", [
    {"model": "jev-1"},                                          # sem answers
    {"answers": None},
    {"answers": ["ok"]},
    [],
    {"answers": {"ok": "0.8"}},                                  # resposta não é objeto
    {"answers": {"ok": {"type": "noul"}}},                       # sem o valor
    {"answers": {"ok": {"type": "noul", "noul": None}}},         # None
    {"answers": {"ok": {"type": "noul", "noul": "alta"}}},       # str inválida
    {"answers": {"ok": {"type": "noul", "noul": 7}}},            # fora de [0, 1]
    {"answers": {"ok": {"type": "desconhecido"}}},
    {"answers": {}},                                             # pergunta sem resposta
    {"answers": {"ok": {"type": "noul", "noul": 0.8}}, "usage": {"input_tokens": "muitos"}},
], ids=["sem-answers", "answers-none", "answers-lista", "lista", "answer-str", "sem-valor", "noul-none",
        "noul-str", "noul-fora", "tipo", "pergunta-faltando", "usage-invalido"])
def test_malformed_payload_raises_typed_error(payload):
    with pytest.raises(JevMalformedResponseError):
        _client(Recorder(payload)).ask("s", QUESTIONS)


def test_non_json_body_raises_typed_error():
    recorder = Recorder(httpx.Response(200, text="<html>gateway</html>"))
    with pytest.raises(JevMalformedResponseError):
        _client(recorder).ask("s", QUESTIONS)


def test_all_client_errors_share_a_base_and_never_leak_key_error():
    for step in (httpx.Response(500), httpx.ConnectError("x"), {"nada": 1}):
        with pytest.raises(JevError) as info:
            _client(Recorder(step), max_retries=0).ask("s", QUESTIONS)
        assert not isinstance(info.value, KeyError)


def test_retry_after_parsing():
    now = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)
    assert retry_after_seconds("3") == 3.0
    assert retry_after_seconds("-1") == 0.0
    assert retry_after_seconds("Wed, 23 Sep 2026 12:00:05 GMT", now=lambda: now) == 5.0
    assert retry_after_seconds("amanhã") is None
    assert retry_after_seconds(None) is None


def test_emulator_drops_invalid_noul_instead_of_inventing_half():
    from conftest import ScriptedLLM
    from langchain_core.messages import AIMessage

    raw = {"a": {"p_true": None}, "b": {"p_true": "alta"}, "d": {"p_true": 0.3}}  # "c" ausente
    llm = ScriptedLLM(script=[AIMessage(content=json.dumps(raw))])
    questions = {key: NoulQuestion(instructions="?") for key in "abcd"}
    response = EmulatedJevClient(llm).ask("s", questions)
    assert set(response.answers) == {"d"}
    assert response.noul("d") == 0.3


# ---------- o erro do cliente leva o fluxo ao caminho humano ----------

def _failing_client(step) -> RealJevClient:
    return _client(Recorder(step), max_retries=1)


@pytest.mark.parametrize("step", [httpx.Response(503), httpx.ConnectTimeout("t"), {"model": "x"}],
                         ids=["5xx", "timeout", "sem-answers"])
def test_client_error_escalates_to_human_without_llm(make_runner, step):
    runner = make_runner([], jev=_failing_client(step))  # roteiro vazio: LLM não pode ser chamado
    ingest_only(runner, MARIANA)
    final = runner.process_item(MARIANA)

    assert final["status"] == "escalated"
    assert "triagem indisponível" in final["escalation_reason"]
    assert runner.llm.calls == 0
    assert runner.adapters.email.sent == []
    assert runner.store.get_item(MARIANA)["status"] == "escalated"


def test_client_error_uses_emulated_fallback_when_configured(make_runner):
    from test_resilience import HAPPY

    fallback = FakeJev()
    runner = make_runner(HAPPY, jev=_failing_client(httpx.Response(500)), jev_fallback=fallback)
    ingest_only(runner, MARIANA)
    final = runner.process_item(MARIANA)

    assert final["status"] == "sent"
    assert any("Jev indisponível (JevUnavailableError)" in note for note in final["notes"])
    assert len(fallback.calls) == 3              # triagem, gate e verificação pelo fallback
