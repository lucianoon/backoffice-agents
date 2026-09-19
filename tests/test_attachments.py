"""Anexos: extração de PDF, texto e imagem, e presença no estado do item."""

import base64
from pathlib import Path

from conftest import ScriptedLLM, ingest_only
from langchain_core.messages import AIMessage, messages_from_dict

from backoffice_agents.adapters.email import Attachment
from backoffice_agents.attachments import extract_text

PDF = (Path(__file__).parent / "fixtures" / "comprovante.pdf").read_bytes()


def _att(name, ctype, data: bytes, size=None):
    return Attachment(filename=name, content_type=ctype, data_b64=base64.b64encode(data).decode(),
                      size=size or len(data))


def test_pdf_text_is_extracted():
    result = extract_text(_att("comprovante.pdf", "application/pdf", PDF))
    assert result["method"] == "pypdf"
    assert "NF-56020" in result["text"] and "189,90" in result["text"]


def test_text_and_unsupported_and_oversized():
    assert extract_text(_att("obs.txt", "text/plain", "Olá mundo".encode()))["text"] == "Olá mundo"
    assert "não suportado" in extract_text(_att("a.zip", "application/zip", b"PK"))["text"]
    big = Attachment(filename="video.mp4", content_type="video/mp4", size=99_000_000)
    assert extract_text(big)["method"] == "skipped"


def test_image_without_llm_and_with_llm():
    png = _att("foto.png", "image/png", b"\x89PNG fake")
    assert extract_text(png)["method"] == "none"
    llm = ScriptedLLM(script=[AIMessage(content="Tampo rachado no canto esquerdo.")])
    result = extract_text(png, llm)
    assert result["method"] == "llm-vision" and "rachado" in result["text"]


def test_corrupt_pdf_does_not_raise():
    result = extract_text(_att("x.pdf", "application/pdf", b"%PDF-1.4 lixo"))
    assert result["method"] in {"error", "pypdf"} and result["text"]


def test_attachment_text_reaches_triage_prompt_and_verification(make_runner):
    runner = make_runner([AIMessage(content="Pagamento confirmado.\nEquipe de Atendimento")])
    runner.ingest_emails()
    # em-108 (com comprovante em PDF) está em emails_eval.json: injeta direto como item
    import json
    eval_path = Path(__file__).resolve().parents[1] / "data" / "samples" / "emails_eval.json"
    email = next(e for e in json.loads(eval_path.read_text(encoding="utf-8")) if e["id"] == "em-108")
    runner.store.upsert_item("email:em-108", "email", "new", email)
    ingest_only(runner, "email:em-108")
    final = runner.process_item("email:em-108")

    assert final["attachments"][0]["filename"] == "comprovante.pdf"
    assert "NF-56020" in final["attachments"][0]["text"]
    assert "NF-56020" in runner.jev.states[0]["attachments"][0]["text"]          # triagem
    assert "ANEXOS" in messages_from_dict(final["messages"])[1].content           # prompt do agente
    assert runner.jev.states[-1]["attachments"][0]["filename"] == "comprovante.pdf"  # verificação
    assert any("comprovante.pdf (pypdf)" in n for n in final["notes"])
