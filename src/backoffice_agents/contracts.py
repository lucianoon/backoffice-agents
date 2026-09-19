"""Suíte de contrato dos adapters.

Os mesmos checks rodam contra os mocks (na suíte de testes e no CI) e contra os sistemas reais
(`backoffice contracts`), para que o grafo não precise saber a diferença. Contra sistemas reais,
as operações de escrita só rodam com `allow_writes=True`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .adapters import Adapters
from .adapters.crm import Contact, Deal, Interaction
from .adapters.email import EmailMessage
from .adapters.erp import Invoice, Order, StockLevel
from .adapters.telegram import Button


@dataclass
class ContractResult:
    adapter: str
    passed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


def _run(result: ContractResult, name: str, check) -> None:
    try:
        check()
    except AssertionError as exc:
        result.failed.append((name, str(exc) or "asserção falhou"))
    except Exception as exc:
        result.failed.append((name, f"{exc.__class__.__name__}: {exc}"))
    else:
        result.passed.append(name)


def check_crm(crm, known_email: str, allow_writes: bool = False) -> ContractResult:
    result = ContractResult("crm")

    def unknown_contact_is_none():
        assert crm.find_contact_by_email("ninguem-" + known_email) is None

    def known_contact_has_shape():
        contact = crm.find_contact_by_email(known_email)
        assert isinstance(contact, Contact), "find_contact_by_email deve devolver Contact"
        assert contact.id and contact.email.lower() == known_email.lower()

    def lookup_is_case_insensitive():
        assert crm.find_contact_by_email(known_email.upper()) is not None

    def open_deals_are_deals():
        contact = crm.find_contact_by_email(known_email)
        deals = crm.open_deals(contact.id)
        assert isinstance(deals, list) and all(isinstance(d, Deal) for d in deals)
        assert all(d.stage != "fechado" for d in deals), "open_deals não pode devolver fechadas"

    def log_interaction_returns_record():
        contact = crm.find_contact_by_email(known_email)
        item = crm.log_interaction(contact.id, "contract", "teste de contrato")
        assert isinstance(item, Interaction) and item.id and item.at

    for name, check in [("contato desconhecido -> None", unknown_contact_is_none),
                        ("contato conhecido tem forma", known_contact_has_shape),
                        ("busca ignora maiúsculas", lookup_is_case_insensitive),
                        ("oportunidades abertas", open_deals_are_deals)]:
        _run(result, name, check)
    if allow_writes:
        _run(result, "registrar interação", log_interaction_returns_record)
    else:
        result.skipped.append("registrar interação (escrita)")
    return result


def check_erp(erp, known_order: str, known_sku: str, customer_email: str,
              allow_writes: bool = False) -> ContractResult:
    result = ContractResult("erp")

    def unknown_order_is_none():
        assert erp.get_order("PED-00000000") is None

    def known_order_has_shape():
        order = erp.get_order(known_order)
        assert isinstance(order, Order) and order.items and order.total >= 0
        assert order.status in {"aguardando_pagamento", "em_separacao", "enviado", "entregue", "cancelado"}

    def order_lookup_is_case_insensitive():
        assert erp.get_order(known_order.lower()) is not None

    def list_orders_matches_customer():
        orders = erp.list_orders(customer_email)
        assert all(isinstance(o, Order) and o.customer_email.lower() == customer_email.lower()
                   for o in orders)

    def invoices_reference_orders():
        for invoice in erp.list_invoices(customer_email):
            assert isinstance(invoice, Invoice) and invoice.status in {"aberta", "paga", "vencida"}
            assert erp.get_order(invoice.order_id) is not None, "fatura aponta para pedido inexistente"

    def stock_has_shape():
        level = erp.check_stock(known_sku)
        assert isinstance(level, StockLevel) and level.available >= 0 and level.unit_price > 0
        assert erp.check_stock("SKU-NAO-EXISTE") is None

    def create_order_decrements_stock():
        before = erp.check_stock(known_sku).available
        assert before >= 1, "sem estoque para testar criação"
        order = erp.create_order(customer_email, known_sku, 1)
        assert order.status == "aguardando_pagamento" and erp.get_order(order.id) is not None
        assert erp.check_stock(known_sku).available == before - 1
        cancelled = erp.cancel_order(order.id, "teste de contrato")
        assert cancelled.status == "cancelado"

    def cancel_shipped_is_refused():
        shipped = next((o for o in erp.list_orders(customer_email) if o.status in {"enviado", "entregue"}),
                       None)
        if shipped is None:
            return
        try:
            erp.cancel_order(shipped.id, "teste")
        except ValueError:
            return
        raise AssertionError("cancelar pedido enviado deveria falhar com ValueError")

    for name, check in [("pedido desconhecido -> None", unknown_order_is_none),
                        ("pedido conhecido tem forma", known_order_has_shape),
                        ("busca de pedido ignora maiúsculas", order_lookup_is_case_insensitive),
                        ("pedidos do cliente", list_orders_matches_customer),
                        ("faturas apontam para pedidos", invoices_reference_orders),
                        ("estoque tem forma", stock_has_shape)]:
        _run(result, name, check)
    if allow_writes:
        _run(result, "criar pedido baixa estoque e cancela", create_order_decrements_stock)
        _run(result, "cancelar pedido enviado é recusado", cancel_shipped_is_refused)
    else:
        result.skipped += ["criar pedido (escrita)", "cancelar pedido enviado (escrita)"]
    return result


def check_email(email, allow_writes: bool = False) -> ContractResult:
    result = ContractResult("email")

    def fetch_returns_messages():
        messages = email.fetch_unread()
        assert isinstance(messages, list)
        for m in messages:
            assert isinstance(m, EmailMessage) and m.id and m.from_addr and "@" in m.from_addr

    def fetch_is_repeatable_until_marked():
        first = {m.id for m in email.fetch_unread()}
        second = {m.id for m in email.fetch_unread()}
        assert first == second, "fetch_unread sem mark_processed deve ser idempotente"

    def send_reply_accepts_message():
        messages = email.fetch_unread()
        assert messages, "sem mensagem para responder"
        email.send_reply(messages[0], "Resposta de teste de contrato.\nEquipe de Atendimento")

    _run(result, "fetch devolve EmailMessage", fetch_returns_messages)
    _run(result, "fetch é idempotente até marcar", fetch_is_repeatable_until_marked)
    if allow_writes:
        _run(result, "responder", send_reply_accepts_message)
    else:
        result.skipped.append("responder (escrita)")
    return result


def check_telegram(telegram, allow_writes: bool = False) -> ContractResult:
    result = ContractResult("telegram")

    def get_updates_returns_list():
        assert isinstance(telegram.get_updates(None), list)

    def send_returns_message_id():
        message_id = telegram.send_message("teste de contrato",
                                           [Button(text="ok", callback_data="noop:0")])
        assert isinstance(message_id, str) and message_id

    _run(result, "get_updates devolve lista", get_updates_returns_list)
    if allow_writes:
        _run(result, "enviar mensagem com botão", send_returns_message_id)
    else:
        result.skipped.append("enviar mensagem (escrita)")
    return result


def check_all(adapters: Adapters, known_email: str, known_order: str, known_sku: str,
              allow_writes: bool = False) -> list[ContractResult]:
    return [
        check_crm(adapters.crm, known_email, allow_writes),
        check_erp(adapters.erp, known_order, known_sku, known_email, allow_writes),
        check_email(adapters.email, allow_writes),
        check_telegram(adapters.telegram, allow_writes),
    ]
