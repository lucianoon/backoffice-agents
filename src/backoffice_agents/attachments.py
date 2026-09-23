"""Extração de texto de anexos: PDF (pypdf), texto/CSV e imagens (transcrição pelo LLM com visão).

O resultado entra no estado do item como dado, ao lado do corpo do e-mail, e passa pela mesma
pseudonimização antes de ir ao Jev. A triagem chama `extract_all` primeiro sem LLM e só transcreve
imagens depois que o e-mail passou pelo gate de prompt injection (ver `Nodes.triage`).
"""

from __future__ import annotations

import io
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage

from .adapters.email import Attachment

MAX_CHARS = 6000
IMAGE_PROMPT = ("Transcreva fielmente todo o texto legível desta imagem, em português, preservando "
                "números, valores, datas e códigos. Se for uma foto sem texto, descreva o que mostra "
                "em uma frase objetiva. Não interprete nem opine.")


def extract_text(attachment: Attachment, llm: BaseChatModel | None = None) -> dict[str, Any]:
    """Devolve {filename, content_type, text, method}. Nunca levanta: erro vira texto explicativo."""
    kind = attachment.content_type.lower()
    base = {"filename": attachment.filename, "content_type": attachment.content_type}
    if not attachment.data_b64:
        return base | {"text": f"[anexo ignorado: {attachment.size} bytes acima do limite]",
                       "method": "skipped"}
    try:
        if kind == "application/pdf" or attachment.filename.lower().endswith(".pdf"):
            return base | {"text": _pdf_text(attachment.data), "method": "pypdf"}
        if kind.startswith("text/") or kind in {"application/json", "application/xml"}:
            return base | {"text": _clip(attachment.data.decode("utf-8", "replace")), "method": "decode"}
        if kind.startswith("image/"):
            if llm is None:
                return base | {"text": "[imagem: transcrição indisponível sem LLM com visão]",
                               "method": "none"}
            return base | {"text": _image_text(attachment, llm), "method": "llm-vision"}
        return base | {"text": f"[tipo {attachment.content_type} não suportado]", "method": "none"}
    except Exception as exc:
        return base | {"text": f"[falha ao extrair: {exc.__class__.__name__}]", "method": "error"}


def extract_all(attachments: list[Attachment], llm: BaseChatModel | None = None) -> list[dict[str, Any]]:
    return [extract_text(a, llm) for a in attachments]


def needs_vision(attachments: list[Attachment]) -> bool:
    """Algum anexo só vira texto com o LLM com visão? (imagem com conteúdo; PDF e texto não)."""
    return any(a.data_b64 and a.content_type.lower().startswith("image/")
               and not a.filename.lower().endswith(".pdf") for a in attachments)


def _clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + "\n[... truncado ...]"


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    text = "\n\n".join(p for p in pages if p)
    return _clip(text) if text else "[PDF sem texto extraível: provavelmente digitalizado; exige OCR]"


def _image_text(attachment: Attachment, llm: BaseChatModel) -> str:
    message = HumanMessage(content=[
        {"type": "text", "text": IMAGE_PROMPT},
        {"type": "image_url",
         "image_url": {"url": f"data:{attachment.content_type};base64,{attachment.data_b64}"}},
    ])
    response = llm.invoke([message])
    content = response.content if isinstance(response.content, str) else str(response.content)
    return _clip(content)
