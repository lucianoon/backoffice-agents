from __future__ import annotations

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


def build_adapters(settings: Settings) -> Adapters:
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

    return Adapters(email=email, crm=MockCrmAdapter(), erp=MockErpAdapter(), telegram=telegram)
