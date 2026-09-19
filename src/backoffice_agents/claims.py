"""Resposta do agente em duas partes: o texto ao cliente e a lista de fatos que ele afirma.

O prompt pede, depois do texto, uma linha `---FATOS---` seguida de um fato por linha. Cada fato
vira uma pergunta sim/não ao Jev na verificação ("este fato tem base nos dados?"), o que dá um
sinal por afirmação e uma explicação concreta quando o rascunho é reprovado.
"""

from __future__ import annotations

import re

DELIMITER = re.compile(r"^\s*-{2,}\s*FATOS\s*-{2,}\s*$|^\s*FATOS\s*:\s*$", re.IGNORECASE | re.MULTILINE)
MAX_CLAIMS = 10


def split_reply(text: str) -> tuple[str, list[str]]:
    """Devolve (texto ao cliente, fatos). Sem delimitador, o texto inteiro é a resposta e não há fatos."""
    match = DELIMITER.search(text or "")
    if not match:
        return (text or "").strip(), []
    reply = text[: match.start()].strip()
    claims = []
    for line in text[match.end():].splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
        if line:
            claims.append(line)
    return reply, claims[:MAX_CLAIMS]
