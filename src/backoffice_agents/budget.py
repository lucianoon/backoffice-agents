"""Orçamento de tokens do estado enviado ao Jev.

O Jev aceita ~32k tokens para estado + maior pergunta (64k por requisição). Threads longas,
anexos e resultados de ferramentas estouram isso. `fit_state` encolhe o estado em passos
previsíveis, do menos para o mais importante, até caber no orçamento, e diz o que cortou.
"""

from __future__ import annotations

import json
from typing import Any

CHARS_PER_TOKEN = 3.0  # conservador para português; o Jev não publica o tokenizador
OMITTED = "[omitido por limite de tokens]"


def estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return int(len(text) / CHARS_PER_TOKEN) + 1


def _clip(text: str, max_chars: int, tail: int = 0) -> str:
    if len(text) <= max_chars:
        return text
    if tail:
        return text[: max_chars - tail] + "\n[...]\n" + text[-tail:]
    return text[:max_chars] + " [...]"


def _clip_history(state: dict[str, Any], keep: int) -> bool:
    history = state.get("conversation_history")
    if not history or len(history) <= keep:
        return False
    state["conversation_history"] = history[-keep:] if keep else []
    if not keep:
        state.pop("conversation_history", None)
        state["conversation_history_note"] = OMITTED
    return True


def _clip_attachments(state: dict[str, Any], max_chars: int) -> bool:
    changed = False
    for att in state.get("attachments") or []:
        text = att.get("text", "")
        if len(text) > max_chars:
            att["text"] = _clip(text, max_chars) if max_chars else OMITTED
            changed = True
    return changed


def _clip_facts(state: dict[str, Any], max_chars: int, keep: int | None = None) -> bool:
    facts = state.get("data_gathered") or state.get("data_gathered_so_far")
    key = "data_gathered" if "data_gathered" in state else "data_gathered_so_far"
    if not facts:
        return False
    changed = False
    if keep is not None and len(facts) > keep:
        facts = facts[-keep:]
        changed = True
    for fact in facts:
        result = fact.get("result")
        dumped = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        if len(dumped) > max_chars:
            fact["result"] = _clip(dumped, max_chars)
            changed = True
    state[key] = facts
    return changed


def _clip_email(state: dict[str, Any], max_chars: int) -> bool:
    for key in ("email", "customer_request"):
        block = state.get(key)
        if isinstance(block, dict) and len(block.get("body", "")) > max_chars:
            block["body"] = _clip(block["body"], max_chars, tail=max_chars // 4)
            return True
    return False


# Passos de redução, do menos para o mais importante. Cada um devolve True se mudou algo.
STEPS: list[tuple[str, Any]] = [
    ("histórico: só as 3 últimas mensagens", lambda s: _clip_history(s, 3)),
    ("anexos: 2000 caracteres cada", lambda s: _clip_attachments(s, 2000)),
    ("fatos das ferramentas: 1500 caracteres cada", lambda s: _clip_facts(s, 1500)),
    ("histórico: só a última mensagem", lambda s: _clip_history(s, 1)),
    ("anexos: 500 caracteres cada", lambda s: _clip_attachments(s, 500)),
    ("fatos das ferramentas: 400 caracteres, só os 5 últimos", lambda s: _clip_facts(s, 400, keep=5)),
    ("corpo do e-mail: 4000 caracteres", lambda s: _clip_email(s, 4000)),
    ("histórico omitido", lambda s: _clip_history(s, 0)),
    ("anexos omitidos", lambda s: _clip_attachments(s, 0)),
    ("corpo do e-mail: 1500 caracteres", lambda s: _clip_email(s, 1500)),
]


def fit_state(state: dict[str, Any], budget_tokens: int) -> tuple[dict[str, Any], list[str]]:
    """Devolve (estado que cabe no orçamento, lista do que foi cortado). Não altera o original."""
    state = json.loads(json.dumps(state, ensure_ascii=False))
    applied: list[str] = []
    if estimate_tokens(state) <= budget_tokens:
        return state, applied
    for label, step in STEPS:
        if step(state):
            applied.append(label)
        if estimate_tokens(state) <= budget_tokens:
            return state, applied
    # último recurso: corta as maiores strings restantes pela metade até caber
    while estimate_tokens(state) > budget_tokens:
        path, text = _longest_string(state)
        if text is None or len(text) < 200:
            break
        _set_path(state, path, _clip(text, len(text) // 2))
        applied.append(f"corte bruto em {'.'.join(map(str, path))}")
    return state, applied


def _longest_string(obj: Any, path: tuple = ()) -> tuple[tuple, str | None]:
    best: tuple[tuple, str | None] = ((), None)
    if isinstance(obj, str):
        return path, obj
    items = obj.items() if isinstance(obj, dict) else enumerate(obj) if isinstance(obj, list) else []
    for key, value in items:
        candidate = _longest_string(value, (*path, key))
        if candidate[1] is not None and (best[1] is None or len(candidate[1]) > len(best[1])):
            best = candidate
    return best


def _set_path(obj: Any, path: tuple, value: Any) -> None:
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value
