"""Ferramentas do agente com nível de risco declarado.

O LLM só vê nome, descrição e argumentos. O nível de risco fica no registro e é
consultado pelo gate antes de qualquer execução.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from .adapters import Adapters
from .policy import RiskLevel


@dataclass
class ToolContext:
    """Estado mutável de um item durante a execução (o que as tools podem alterar)."""
    customer_email: str
    escalation_reason: str | None = None
    forwarded: list[dict[str, str]] = field(default_factory=list)
    kb: Any = None                                  # KnowledgeBase (opcional)
    log_jev: Callable[[str, Any], None] | None = None  # registra as chamadas ao Jev feitas por tools


@dataclass
class ToolRegistry:
    tools: list[StructuredTool]
    risk: dict[str, RiskLevel]
    context: ToolContext

    def by_name(self, name: str) -> StructuredTool:
        return next(t for t in self.tools if t.name == name)


class _Email(BaseModel):
    email: str = Field(description="e-mail do cliente")


class _OrderId(BaseModel):
    order_id: str = Field(description="número do pedido, ex.: PED-78231")


class _InvoiceId(BaseModel):
    invoice_id: str = Field(description="número da nota/fatura, ex.: NF-55120")


class _Sku(BaseModel):
    sku: str = Field(description="código do produto, ex.: SKU-1001")


class _LogInteraction(BaseModel):
    email: str = Field(description="e-mail do cliente")
    summary: str = Field(description="resumo objetivo da interação, 1 a 2 frases")


class _CreateDeal(BaseModel):
    email: str = Field(description="e-mail do cliente")
    title: str = Field(description="título da oportunidade")
    value: float = Field(description="valor estimado em reais")


class _CreateOrder(BaseModel):
    email: str = Field(description="e-mail do cliente")
    sku: str = Field(description="código do produto")
    quantity: int = Field(ge=1, description="quantidade")


class _CancelOrder(BaseModel):
    order_id: str = Field(description="número do pedido")
    reason: str = Field(description="motivo informado pelo cliente")


class _Forward(BaseModel):
    to: str = Field(description="e-mail do setor/pessoa de destino")
    note: str = Field(description="contexto curto para quem vai receber")


class _Escalate(BaseModel):
    reason: str = Field(description="por que um humano precisa assumir")


class _Query(BaseModel):
    query: str = Field(description="pergunta em linguagem natural, "
                                   "ex.: 'prazo para troca de produto com defeito'")


def build_tools(adapters: Adapters, context: ToolContext) -> ToolRegistry:
    crm, erp = adapters.crm, adapters.erp

    def _dump(obj: Any) -> Any:
        if obj is None:
            return {"found": False}
        if isinstance(obj, list):
            return [o.model_dump() for o in obj]
        return obj.model_dump()

    def crm_find_contact(email: str) -> dict:
        return _dump(crm.find_contact_by_email(email))

    def crm_open_deals(email: str) -> Any:
        contact = crm.find_contact_by_email(email)
        return _dump(crm.open_deals(contact.id)) if contact else {"found": False}

    def crm_log_interaction(email: str, summary: str) -> dict:
        contact = crm.find_contact_by_email(email)
        if not contact:
            return {"ok": False, "error": "contato não encontrado no CRM"}
        return _dump(crm.log_interaction(contact.id, "email", summary))

    def crm_create_deal(email: str, title: str, value: float) -> dict:
        contact = crm.find_contact_by_email(email)
        if not contact:
            return {"ok": False, "error": "contato não encontrado no CRM"}
        return _dump(crm.create_deal(contact.id, title, value))

    def erp_get_order(order_id: str) -> dict:
        return _dump(erp.get_order(order_id))

    def erp_list_orders(email: str) -> Any:
        return _dump(erp.list_orders(email))

    def erp_get_invoice(invoice_id: str) -> dict:
        return _dump(erp.get_invoice(invoice_id))

    def erp_list_invoices(email: str) -> Any:
        return _dump(erp.list_invoices(email))

    def erp_check_stock(sku: str) -> dict:
        return _dump(erp.check_stock(sku))

    def erp_create_order(email: str, sku: str, quantity: int) -> dict:
        try:
            return _dump(erp.create_order(email, sku, quantity))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def erp_cancel_order(order_id: str, reason: str) -> dict:
        try:
            return _dump(erp.cancel_order(order_id, reason))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def email_forward(to: str, note: str) -> dict:
        context.forwarded.append({"to": to, "note": note})
        return {"ok": True, "queued": True}

    def escalate_to_human(reason: str) -> dict:
        context.escalation_reason = reason
        return {"ok": True}

    def kb_search(query: str) -> dict:
        if context.kb is None:
            return {"found": False, "hint": "base de conhecimento não configurada; não invente regra"}
        return context.kb.to_tool_result(context.kb.search(query, on_jev=context.log_jev))

    specs: list[tuple[Callable, type[BaseModel], str, RiskLevel]] = [
        (crm_find_contact, _Email, "Busca o contato do cliente no CRM pelo e-mail.", RiskLevel.LOW),
        (crm_open_deals, _Email, "Lista oportunidades abertas do cliente no CRM.", RiskLevel.LOW),
        (crm_log_interaction, _LogInteraction, "Registra no CRM um resumo desta interação.",
         RiskLevel.MEDIUM),
        (crm_create_deal, _CreateDeal, "Cria uma oportunidade comercial no CRM.", RiskLevel.HIGH),
        (erp_get_order, _OrderId, "Consulta um pedido no ERP pelo número.", RiskLevel.LOW),
        (erp_list_orders, _Email, "Lista os pedidos do cliente no ERP.", RiskLevel.LOW),
        (erp_get_invoice, _InvoiceId, "Consulta uma nota/fatura no ERP.", RiskLevel.LOW),
        (erp_list_invoices, _Email, "Lista as faturas do cliente no ERP.", RiskLevel.LOW),
        (erp_check_stock, _Sku, "Consulta disponibilidade e preço de um produto.", RiskLevel.LOW),
        (erp_create_order, _CreateOrder, "Cria um pedido de venda no ERP.", RiskLevel.HIGH),
        (erp_cancel_order, _CancelOrder, "Cancela um pedido no ERP. Irreversível.", RiskLevel.CRITICAL),
        (email_forward, _Forward, "Encaminha o e-mail do cliente para outro setor.", RiskLevel.MEDIUM),
        (escalate_to_human, _Escalate,
         "Passa o caso para um humano quando não é possível resolver com segurança.", RiskLevel.LOW),
        (kb_search, _Query,
         ("Consulta as políticas internas (trocas, devoluções, prazos de entrega, pagamento, garantia). "
          "Use ANTES de afirmar qualquer prazo, regra ou condição ao cliente."), RiskLevel.LOW),
    ]
    tools = [StructuredTool.from_function(func=fn, name=fn.__name__, description=desc, args_schema=schema)
             for fn, schema, desc, _ in specs]
    risk = {fn.__name__: level for fn, _, _, level in specs}
    return ToolRegistry(tools=tools, risk=risk, context=context)
