"""Rastreamento por item: LangSmith (nativo do LangChain) ou Langfuse (callback).

Cada execução do grafo vira um trace nomeado pelo item, com os nós, as chamadas do LLM e as
ferramentas como spans. As chamadas ao Jev entram como spans do tipo "llm" via `traceable`,
que é no-op quando o rastreamento está desligado.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from langsmith import traceable

from .config import Settings


def configure_tracing(settings: Settings) -> None:
    """Liga o LangSmith por variável de ambiente; o LangChain faz o resto sozinho."""
    if settings.tracing == "langsmith":
        os.environ.setdefault("LANGSMITH_TRACING", "true")
        if settings.langsmith_project:
            os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)


def _langfuse_handler() -> Any:
    try:
        from langfuse.langchain import CallbackHandler  # langfuse >= 3
    except ImportError:
        from langfuse.callback import CallbackHandler  # langfuse 2.x
    return CallbackHandler()


def run_config(settings: Settings, item_id: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Config passada ao `graph.invoke`: nome do trace, tags e callbacks do provedor escolhido."""
    config: dict[str, Any] = {
        "run_name": f"backoffice {item_id}",
        "tags": ["backoffice", f"jev:{settings.jev_mode}"],
        "metadata": {"item_id": item_id, **(metadata or {})},
    }
    if settings.tracing == "langfuse":
        config["callbacks"] = [_langfuse_handler()]
    return config


@traceable(run_type="llm", name="jev",
           process_inputs=lambda inputs: {k: v for k, v in inputs.items() if k != "ask"})
def traced_jev_ask(ask: Callable[..., Any], model: str, payload: Any, questions: dict[str, Any]) -> Any:
    """Executa `ask` dentro de um span "jev"; o `model` fica visível nos inputs do trace."""
    return ask(payload, questions)
