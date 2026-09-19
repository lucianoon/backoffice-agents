from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM
    llm_provider: str = "openai"
    llm_model: str = "gpt-4.1-mini"
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_temperature: float = 0.2

    # Jev
    jev_mode: Literal["real", "emulated"] = "emulated"
    typesafe_api_key: str | None = None
    jev_model: str = "jev-latest"
    jev_base_url: str = "https://api.typesafe.ai/v1/systemone"
    jev_timeout_s: float = 15.0
    jev_anonymize: bool = True          # pseudonimiza o state antes de sair para o Jev
    jev_fallback_emulated: bool = True  # com JEV_MODE=real, cai para o emulador se a API falhar
    max_attempts: int = 3               # reprocessamentos de um item em 'error' antes de 'failed'

    # Política de confiança
    confidence_auto: float = 0.85
    confidence_review: float = 0.55
    gate_min_confidence: float = 0.70
    verify_min_quality: float = 3.5
    verify_min_resolves: float = 0.70
    max_tool_iterations: int = 8
    max_regenerations: int = 1

    # Adapters
    email_adapter: Literal["mock", "imap"] = "mock"
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_user: str | None = None
    imap_password: str | None = None
    imap_folder: str = "INBOX"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    email_from: str | None = None

    crm_adapter: Literal["mock"] = "mock"
    erp_adapter: Literal["mock"] = "mock"

    telegram_adapter: Literal["mock", "bot"] = "mock"
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_mock_auto_approve: bool = True

    # Persistência
    db_path: str = "data/backoffice.db"
    samples_path: str = "data/samples/emails.json"


def get_settings() -> Settings:
    return Settings()
