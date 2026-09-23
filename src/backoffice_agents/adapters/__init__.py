from __future__ import annotations

import importlib
from dataclasses import dataclass

from ..config import Settings
from .crm import CrmAdapter, MockCrmAdapter
from .email import EmailAdapter, ImapSmtpEmailAdapter, MockEmailAdapter
from .erp import ErpAdapter, MockErpAdapter
from .telegram import BotApiTelegramAdapter, MockTelegramAdapter, TelegramAdapter


@dataclass
class Adapters:
    email: EmailAdapter
    crm: CrmAdapter
    erp: ErpAdapter
    telegram: TelegramAdapter


def load_adapter_class(spec: str):
    """Carrega uma classe de adapter. `spec` é 'mock' (não use aqui) ou 'modulo:Classe'."""
    if ":" not in spec:
        raise ValueError(f"adapter {spec!r} deve ser 'mock' ou 'modulo:Classe'")
    module_name, class_name = spec.split(":", 1)
    module = importlib.import_module(module_name)
    try:
        return getattr(module, class_name)
    except AttributeError as exc:
        raise ValueError(f"classe {class_name!r} não existe em {module_name}") from exc


def _build_crm(spec: str) -> CrmAdapter:
    return MockCrmAdapter() if spec == "mock" else load_adapter_class(spec)()


def _build_erp(spec: str) -> ErpAdapter:
    return MockErpAdapter() if spec == "mock" else load_adapter_class(spec)()


def build_adapters(settings: Settings) -> Adapters:
    email: EmailAdapter
    telegram: TelegramAdapter
    if settings.email_adapter == "imap":
        email = ImapSmtpEmailAdapter(settings)
    else:
        email = MockEmailAdapter(settings.samples_path)

    if settings.telegram_adapter == "bot":
        operators = {op.strip() for op in settings.telegram_operators.split(",") if op.strip()}
        telegram = BotApiTelegramAdapter(settings.telegram_bot_token or "", settings.telegram_chat_id or "",
                                         operators)
    else:
        telegram = MockTelegramAdapter(auto_approve=settings.telegram_mock_auto_approve)

    return Adapters(email=email, crm=_build_crm(settings.crm_adapter),
                    erp=_build_erp(settings.erp_adapter), telegram=telegram)
