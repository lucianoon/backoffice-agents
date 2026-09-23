"""Fail-closed: resposta do Jev ausente, malformada ou não numérica nunca abre o caminho do LLM.

Regressão do `_injection` que devolvia 0.0 quando a chave `injection` faltava na resposta: o gate
ficava aberto e o e-mail chegava ao agente. Agora qualquer resposta inválida é tratada como
suspeita e o item escala para um humano sem nenhuma chamada ao LLM.
"""

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeJev, ingest_only, tool_call
from langchain_core.messages import AIMessage

from backoffice_agents.graph.nodes import Nodes, valid_noul
from backoffice_agents.jev.models import ChoiceAnswer, JevResponse, NoulAnswer

ROOT = Path(__file__).resolve().parents[1]
MARIANA = "email:em-001"
REPLY = "Olá, seu pedido PED-78231 foi enviado.\nEquipe de Atendimento"
HAPPY = [
    tool_call("erp_get_order", {"order_id": "PED-78231"}, "c1"),
    tool_call("crm_log_interaction", {"email": "mariana.souza@example.invalid", "summary": "ok"}, "c2"),
    AIMessage(content=REPLY),
]
PNG = {"filename": "foto.png", "content_type": "image/png",
       "data_b64": base64.b64encode(b"\x89PNG fake").decode(), "size": 9}

MISSING = object()
# Valores inválidos que não passariam na validação do pydantic: model_construct simula um cliente
# (ou emulador) que devolve a resposta sem validar.
BAD_VALUES: dict[str, Any] = {
    "ausente": MISSING,
    "none": NoulAnswer.model_construct(noul=None),
    "str-invalida": NoulAnswer.model_construct(noul="alta"),
    "str-numerica": NoulAnswer.model_construct(noul="0.9"),
    "nan": NoulAnswer.model_construct(noul=float("nan")),
    "fora-do-intervalo": NoulAnswer.model_construct(noul=1.7),
    "bool": NoulAnswer.model_construct(noul=False),
    "tipo-errado": ChoiceAnswer(choice="x", probabilities={"x": 1.0}, confidence=1.0),
}


class CorruptingJev(FakeJev):
    """FakeJev que remove ou corrompe respostas em chamadas específicas (0-based por chave)."""

    def __init__(self, corrupt: dict[str, Any], on_calls: set[int] | None = None,
                 overrides: dict[str, Any] | None = None) -> None:
        super().__init__(overrides)
        self.corrupt = corrupt
        self.on_calls = on_calls  # None = todas as chamadas que contêm a chave

    def ask(self, state, questions):
        response = super().ask(state, questions)
        index = len(self.calls) - 1
        answers = dict(response.answers)
        for key, bad in self.corrupt.items():
            if key not in answers:
                continue
            seen = sum(1 for q in self.calls[:index] if key in q)
            if self.on_calls is not None and seen not in self.on_calls:
                continue
            if bad is MISSING:
                del answers[key]
            else:
                answers[key] = bad
        return JevResponse.model_construct(model=response.model, answers=answers, usage={},
                                           latency_ms=response.latency_ms, calibrated=True)


def _assert_blocked_before_llm(runner, final):
    assert final["status"] == "escalated"
    assert "fail-closed" in final["escalation_reason"]
    assert runner.llm.calls == 0                               # nenhuma chamada ao LLM
    assert "messages" not in final or not final["messages"]    # o agente não recebeu prompt
    assert runner.adapters.email.sent == []


@pytest.mark.parametrize("bad", list(BAD_VALUES.values()), ids=list(BAD_VALUES))
def test_injection_helper_treats_invalid_answer_as_unknown(bad):
    answers = {} if bad is MISSING else {"injection": bad}
    response = JevResponse.model_construct(model="m", answers=answers, usage={}, latency_ms=0.0,
                                           calibrated=True)
    assert Nodes._injection(response) is None
    assert valid_noul(response, "injection") is None


def test_injection_helper_keeps_valid_values():
    response = JevResponse(model="m", answers={"injection": NoulAnswer(noul=0.0)})
    assert Nodes._injection(response) == 0.0


@pytest.mark.parametrize("bad", list(BAD_VALUES.values()), ids=list(BAD_VALUES))
def test_invalid_injection_answer_escalates_without_llm(make_runner, bad):
    # roteiro vazio: qualquer chamada ao LLM levantaria AssertionError
    runner = make_runner([], jev=CorruptingJev({"injection": bad}))
    ingest_only(runner, MARIANA)
    final = runner.process_item(MARIANA)

    _assert_blocked_before_llm(runner, final)
    assert "prompt injection" in final["escalation_reason"]
    assert final["triage"]["injection"] is None
    assert len(runner.jev.calls) == 1                          # só a triagem


def _runner_with_image(make_runner, script, jev):
    runner = make_runner(script, jev=jev)
    runner.ingest_emails()
    base = json.loads((ROOT / "data" / "samples" / "emails.json").read_text(encoding="utf-8"))[0]
    email = base | {"id": "em-img", "message_id": "<em-img@example.invalid>", "attachments": [PNG]}
    runner.store.upsert_item("email:em-img", "email", "new", email)
    ingest_only(runner, "email:em-img")
    return runner


def test_missing_injection_skips_image_transcription(make_runner):
    runner = _runner_with_image(make_runner, [], CorruptingJev({"injection": MISSING}))
    final = runner.process_item("email:em-img")

    _assert_blocked_before_llm(runner, final)
    assert final["attachments"][0]["method"] == "none"         # imagem nunca foi ao LLM com visão
    assert len(runner.jev.calls) == 1


def test_missing_injection_on_second_triage_blocks_agent(make_runner):
    transcription = AIMessage(content="Etiqueta de envio com rastreio BR123456789XX.")
    # 1ª triagem limpa; a 2ª (com a transcrição) volta sem a chave injection
    jev = CorruptingJev({"injection": MISSING}, on_calls={1})
    runner = _runner_with_image(make_runner, [transcription], jev)
    final = runner.process_item("email:em-img")

    assert final["status"] == "escalated"
    assert "prompt injection" in final["escalation_reason"]
    assert runner.llm.calls == 1                               # só a transcrição, sem ferramentas
    assert len(jev.calls) == 2
    assert runner.adapters.email.sent == []


@pytest.mark.parametrize("key", ["needs_human", "sensitive", "category", "urgency"])
def test_incomplete_triage_escalates_without_llm(make_runner, key):
    runner = make_runner([], jev=CorruptingJev({key: MISSING}))
    ingest_only(runner, MARIANA)
    final = runner.process_item(MARIANA)

    _assert_blocked_before_llm(runner, final)
    assert "resposta incompleta" in final["escalation_reason"]
    assert any(key in note for note in final["notes"])


@pytest.mark.parametrize("bad", [MISSING, NoulAnswer.model_construct(noul="sim")], ids=["ausente", "str"])
def test_invalid_gate_answer_falls_back_to_human_approval(make_runner, settings, bad):
    settings.telegram_mock_auto_approve = False
    runner = make_runner(HAPPY, jev=CorruptingJev({"appropriate": bad}))
    ingest_only(runner, MARIANA)
    final = runner.process_item(MARIANA)

    assert final["status"] == "awaiting_approval"
    assert final["pending_action"]["call"]["name"] == "crm_log_interaction"
    assert "gate indisponível" in final["pending_action"]["reason"]
    assert runner.adapters.email.sent == []


@pytest.mark.parametrize("key", ["resolves", "unsupported_claims", "quality"])
def test_invalid_verification_answer_escalates_without_sending(make_runner, key):
    runner = make_runner(HAPPY, jev=CorruptingJev({key: MISSING}))
    ingest_only(runner, MARIANA)
    final = runner.process_item(MARIANA)

    assert final["status"] == "escalated"
    assert "verificação" in final["escalation_reason"]
    assert runner.adapters.email.sent == []
