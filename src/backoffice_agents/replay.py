"""Gravação e replay das respostas do LLM, para avaliação determinística no CI.

Em modo `record`, cada chamada ao LLM real é guardada num cassete JSON, indexada pelo hash das
mensagens enviadas. Em modo `replay`, a mesma chamada devolve a resposta gravada sem rede nem
chave de API. Uma chamada que não está no cassete falha com mensagem clara: significa que o
prompt, a taxonomia ou o dataset mudaram e o cassete precisa ser gravado de novo.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ReplayMiss(RuntimeError):
    pass


def _key(messages) -> str:
    payload = [(m.__class__.__name__, m.content if isinstance(m.content, str) else json.dumps(m.content))
               for m in messages]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()


class ReplayChatModel(BaseChatModel):
    """`mode="replay"` responde só pelo cassete; `mode="record"` chama `inner` e grava."""

    cassette_path: str
    mode: str = "replay"
    inner: Any = None
    entries: dict[str, dict[str, Any]] = {}
    hits: int = 0
    misses: int = 0

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        path = Path(self.cassette_path)
        self.entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if self.mode == "record" and self.inner is None:
            raise ValueError("modo record exige um LLM interno")

    @property
    def _llm_type(self) -> str:
        return f"replay-{self.mode}"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        key = _key(messages)
        if key in self.entries:
            self.hits += 1
            entry = self.entries[key]
            message = AIMessage(content=entry["content"], usage_metadata=entry.get("usage") or None)
            return ChatResult(generations=[ChatGeneration(message=message)])
        if self.mode != "record":
            self.misses += 1
            raise ReplayMiss(f"chamada sem gravação no cassete {self.cassette_path} (hash {key[:12]}); "
                             "grave de novo com --record")
        response = self.inner.invoke(messages)
        content = response.content if isinstance(response.content, str) else str(response.content)
        usage = getattr(response, "usage_metadata", None)
        self.entries[key] = {"content": content, "usage": dict(usage) if usage else None}
        self._save()
        self.misses += 1
        message = AIMessage(content=content, usage_metadata=usage)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _save(self) -> None:
        path = Path(self.cassette_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.entries, ensure_ascii=False, indent=1, sort_keys=True),
                        encoding="utf-8")
