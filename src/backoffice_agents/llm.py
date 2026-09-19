"""Factory de LLM. Um único ponto de troca de provedor via .env."""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from .config import Settings


def build_llm(settings: Settings, model: str | None = None) -> BaseChatModel:
    """`model` sobrescreve LLM_MODEL (usado para o emulador mais barato, mesmo provedor)."""
    from langchain.chat_models import init_chat_model

    kwargs: dict = {"temperature": settings.llm_temperature}
    if settings.llm_api_key:
        kwargs["api_key"] = settings.llm_api_key
    if settings.llm_provider == "openai" and settings.llm_base_url:
        # Endpoint OpenAI-compatível (Ollama, Groq, vLLM, OpenRouter). Sem LLM_API_KEY, manda placeholder.
        kwargs["base_url"] = settings.llm_base_url
        kwargs.setdefault("api_key", "not-needed")
    # Sem LLM_API_KEY, o SDK do provedor lê a variável padrão (OPENAI_API_KEY, ANTHROPIC_API_KEY...).

    return init_chat_model(model or settings.llm_model, model_provider=settings.llm_provider, **kwargs)


def build_emulator_llm(settings: Settings, agent_llm: BaseChatModel | None = None) -> BaseChatModel:
    """LLM do emulador do Jev: EMULATOR_MODEL se definido, senão o mesmo do agente."""
    if settings.emulator_model:
        return build_llm(settings, model=settings.emulator_model)
    return agent_llm or build_llm(settings)
