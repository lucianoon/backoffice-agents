"""Checagem da configuração do piloto sem vazar segredos."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import create_engine, text

from .adapters import build_adapters
from .config import Settings
from .jev.client import RealJevClient
from .jev.models import NoulQuestion


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def run_doctor(settings: Settings, ping_jev: bool = False) -> list[Check]:
    key_ok = settings.jev_mode != "real" or bool(settings.typesafe_api_key)
    checks: list[Check] = [
        Check("JEV_MODE", True, settings.jev_mode),
        Check("TYPESAFE_API_KEY", key_ok,
              "preenchida" if settings.typesafe_api_key else
              ("vazia (ok no emulador)" if settings.jev_mode != "real" else "vazia e JEV_MODE=real")),
        Check("EMAIL_ADAPTER", True, settings.email_adapter),
        _imap_check(settings),
        Check("TELEGRAM", _telegram_ready(settings), _telegram_detail(settings)),
        Check("EMAIL_FORWARD_ALLOWLIST", True,
              settings.email_forward_allowlist or "vazia (encaminhamento automático recusado)"),
    ]
    if ping_jev and settings.jev_mode == "real" and settings.typesafe_api_key:
        checks.append(_ping_jev(settings))
    return checks


def run_healthcheck(settings: Settings) -> list[Check]:
    """Checagem leve para o HEALTHCHECK do container: configuração coerente e banco acessível.

    Não chama Jev, IMAP nem Telegram (isso é o `doctor`); roda a cada poucos segundos.
    """
    checks = [Check("TYPESAFE_API_KEY", settings.jev_mode != "real" or bool(settings.typesafe_api_key),
                    settings.jev_mode),
              Check("TELEGRAM", _telegram_ready(settings), _telegram_detail(settings)),
              Check("IMAP", _imap_ready(settings), settings.email_adapter)]
    try:
        engine = create_engine(settings.db_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        finally:
            engine.dispose()
        checks.append(Check("DB", True, engine.dialect.name))
    except Exception as exc:
        checks.append(Check("DB", False, exc.__class__.__name__))
    return checks


def _imap_ready(settings: Settings) -> bool:
    if settings.email_adapter != "imap":
        return True
    return bool(settings.imap_user and settings.imap_password
                and settings.smtp_user and settings.smtp_password)


def _imap_check(settings: Settings) -> Check:
    if settings.email_adapter != "imap":
        return Check("IMAP", True, "mock — para caixa real: EMAIL_ADAPTER=imap e IMAP_*/SMTP_*")
    if not _imap_ready(settings):
        return Check("IMAP", False, "imap escolhido, faltam IMAP_USER/PASSWORD ou SMTP_USER/PASSWORD")
    try:
        build_adapters(settings).email.fetch_unread()
    except Exception as exc:
        return Check("IMAP", False, f"falhou o login/leitura: {exc.__class__.__name__}")
    return Check("IMAP", True, f"ok ({settings.imap_host}/{settings.imap_folder})")


def _telegram_ready(settings: Settings) -> bool:
    if settings.telegram_adapter != "bot":
        return True
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def _telegram_detail(settings: Settings) -> str:
    if settings.telegram_adapter != "bot":
        return "mock"
    if not _telegram_ready(settings):
        return "bot escolhido, faltam TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID"
    return "token e chat preenchidos"


def _ping_jev(settings: Settings) -> Check:
    try:
        jev = RealJevClient(settings.typesafe_api_key or "", settings.jev_model,
                            settings.jev_base_url, settings.jev_timeout_s)
        response = jev.ask("ping", {"ok": NoulQuestion(instructions="Is this a connectivity check?",
                                                       criteria={"true": "always true", "false": "never"})})
        return Check("Jev API", True, f"{response.model} {response.latency_ms:.0f} ms")
    except Exception as exc:
        return Check("Jev API", False, f"{exc.__class__.__name__}")
