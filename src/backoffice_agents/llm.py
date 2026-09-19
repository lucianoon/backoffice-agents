"""Factory de LLM. Um único ponto de troca de provedor via .env."""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from .config import Settings


def build_llm(settings: Settings) -> BaseChatModel:
    from langchain.chat_models import init_chat_model

    kwargs: dict = {"temperature": settings.llm_temperature}
    if settings.llm_api_key:
        kwargs["api_key"] = settings.llm_api_key
    if settings.llm_provider == "openai" and settings.llm_base_url:
        # Endpoint OpenAI-compatível (Ollama, Groq, vLLM, OpenRouter). Sem LLM_API_KEY, manda placeholder.
        kwargs["base_url"] = settings.llm_base_url
        kwargs.setdefault("api_key", "not-needed")
    # Sem LLM_API_KEY, o SDK do provedor lê a variável padrão (OPENAI_API_KEY, ANTHROPIC_API_KEY...).

    return init_chat_model(settings.llm_model, model_provider=settings.llm_provider, **kwargs)
