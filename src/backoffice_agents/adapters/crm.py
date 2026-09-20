from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel


class Contact(BaseModel):
    id: str
    name: str
    email: str
    company: str = ""
    segment: str = ""
    since: str = ""


class Deal(BaseModel):
    id: str
    contact_id: str
    title: str
    value: float
    stage: str = "novo"


class Interaction(BaseModel):
    id: str
    contact_id: str
    channel: str
    summary: str
    at: str


class CrmAdapter(Protocol):
    def find_contact_by_email(self, email: str) -> Contact | None: ...
    def open_deals(self, contact_id: str) -> list[Deal]: ...
    def create_deal(self, contact_id: str, title: str, value: float) -> Deal: ...
    def log_interaction(self, contact_id: str, channel: str, summary: str) -> Interaction: ...


class MockCrmAdapter:
    def __init__(self) -> None:
        self.contacts: dict[str, Contact] = {
            "c-001": Contact(id="c-001", name="Mariana Souza", email="mariana.souza@lojaazul.com.br",
                             company="Loja Azul Ltda", segment="varejo", since="2024-03-10"),
            "c-002": Contact(id="c-002", name="Carlos Pereira", email="carlos@construtorapereira.com",
                             company="Construtora Pereira", segment="construção", since="2023-08-22"),
            "c-003": Contact(id="c-003", name="Ana Lima", email="ana.lima@gmail.com",
                             company="", segment="pessoa física", since="2025-11-02"),
            # piloto: contato do lote de e-mails reais de teste
            "c-010": Contact(id="c-010", name="Piloto Backoffice", email="prradical@gmail.com",
                             company="", segment="pessoa física", since="2025-06-01"),
        }
        self.deals: dict[str, Deal] = {
            "d-100": Deal(id="d-100", contact_id="c-002", title="Renovação contrato anual", value=48000.0,
                          stage="proposta"),
        }
        self.interactions: list[Interaction] = []
        self._ids = itertools.count(1000)

    def find_contact_by_email(self, email: str) -> Contact | None:
        email = email.strip().lower()
        return next((c for c in self.contacts.values() if c.email.lower() == email), None)

    def open_deals(self, contact_id: str) -> list[Deal]:
        return [d for d in self.deals.values() if d.contact_id == contact_id and d.stage != "fechado"]

    def create_deal(self, contact_id: str, title: str, value: float) -> Deal:
        deal = Deal(id=f"d-{next(self._ids)}", contact_id=contact_id, title=title, value=value)
        self.deals[deal.id] = deal
        return deal

    def log_interaction(self, contact_id: str, channel: str, summary: str) -> Interaction:
        item = Interaction(id=f"i-{next(self._ids)}", contact_id=contact_id, channel=channel,
                           summary=summary, at=datetime.now(UTC).isoformat(timespec="seconds"))
        self.interactions.append(item)
        return item
