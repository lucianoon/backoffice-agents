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
    retry_delay_s: float = 60.0         # espera mínima antes de reprocessar um item em 'error'

    # Política de confiança
    confidence_auto: float = 0.85
    confidence_review: float = 0.55
    # Sobrescrevem os globais por categoria de triagem. No .env, JSON:
    # CONFIDENCE_AUTO_BY_CATEGORY={"cancelamento": 0.95, "spam_irrelevante": 0.9}
    confidence_auto_by_category: dict[str, float] = {}
    confidence_review_by_category: dict[str, float] = {}
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
    # ids de usuário do Telegram que podem aprovar/rotular (vazio = qualquer um no chat autorizado)
    telegram_operators: str = ""

    # Prompt injection: probabilidade (Noul da triagem) acima da qual o item escala sem passar pelo LLM
    injection_escalate: float = 0.7

    # Base de conhecimento de políticas (arquivos .md); o Jev pontua a relevância dos trechos
    kb_dir: str = "data/kb"
    kb_candidates: int = 8       # trechos da busca lexical enviados ao Jev numa só chamada
    kb_top_k: int = 3            # trechos que chegam ao LLM
    kb_min_score: float = 2.5    # relevância mínima (escala 1 a 4)

    # Threads: e-mails do mesmo remetente com o mesmo assunto normalizado dentro deste prazo
    thread_window_days: int = 14

    # Orçamento de tokens do estado enviado ao Jev (limite da API: ~32k para estado + maior pergunta)
    jev_state_budget_tokens: int = 24000

    # Configuração por cliente: taxonomia, níveis, prompt, assinatura (tenants/*.toml)
    tenant_file: str = "tenants/default.toml"

    # Retenção (LGPD): dias após o encerramento do item
    retention_redact_days: int = 30       # remove conteúdo do cliente, mantém metadados
    retention_delete_days: int = 180      # apaga o item e tudo o que aponta para ele

    # Concorrência dentro do worker e limite de requisições ao Jev (a API publica 1.200/min)
    worker_concurrency: int = 1
    jev_max_rpm: int = 600

    # Modelo mais barato só para o emulador do Jev (vazio = mesmo LLM do agente)
    emulator_model: str | None = None
    emulator_price_input_per_m: float | None = None
    emulator_price_output_per_m: float | None = None

    # Observabilidade
    log_format: Literal["text", "json"] = "text"
    log_level: str = "INFO"
    metrics_namespace: str = "BackofficeAgents"      # CloudWatch (extra 'aws')
    aws_region: str | None = None
    alert_approval_max_age_min: float = 60.0         # aprovação pendente há mais que isso -> aviso
    alert_queue_depth: int = 50
    alert_error_rate: float = 0.2
    alert_cooldown_min: float = 60.0                 # não repete o mesmo aviso antes disso

    # Persistência e fila. Postgres: postgresql+psycopg://user:pass@host:5432/backoffice
    db_url: str = "sqlite:///data/backoffice.db"
    samples_path: str = "data/samples/emails.json"

    # Custo (US$ por milhão de tokens) para o relatório `backoffice costs`
    llm_price_input_per_m: float = 0.40    # padrão: gpt-4.1-mini
    llm_price_output_per_m: float = 1.60
    jev_price_input_per_m: float = 0.042   # docs.typesafe.ai; saída é grátis

    # Rastreamento: none | langsmith (LANGSMITH_API_KEY) | langfuse (LANGFUSE_*_KEY, extra 'langfuse')
    tracing: Literal["none", "langsmith", "langfuse"] = "none"
    langsmith_project: str | None = "backoffice-agents"


def get_settings() -> Settings:
    return Settings()
