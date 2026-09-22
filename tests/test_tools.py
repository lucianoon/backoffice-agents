"""Ferramentas amarradas ao cliente do item e allowlist de encaminhamento."""

from backoffice_agents.adapters import build_adapters
from backoffice_agents.config import Settings
from backoffice_agents.tools import (
    FOREIGN_CUSTOMER,
    ToolContext,
    build_tools,
    forward_allowed,
    parse_allowlist,
)

MARIANA = "mariana.souza@example.invalid"
ANA = "ana.lima@example.invalid"


def _registry(email: str = MARIANA, allowlist: str = ""):
    adapters = build_adapters(Settings(_env_file=None))
    context = ToolContext(customer_email=email, forward_allowlist=parse_allowlist(allowlist))
    return build_tools(adapters, context), adapters


def test_crm_rejects_other_customer_email():
    registry, adapters = _registry()
    result = registry.by_name("crm_find_contact").invoke({"email": ANA})
    assert result["ok"] is False and FOREIGN_CUSTOMER in result["error"]
    assert adapters.crm.find_contact_by_email(ANA) is not None  # o CRM tem o contato; a tool recusou


def test_crm_reads_own_customer():
    registry, _ = _registry()
    result = registry.by_name("crm_find_contact").invoke({"email": MARIANA})
    assert result["email"] == MARIANA


def test_erp_hides_foreign_order_without_leaking():
    registry, _ = _registry()
    result = registry.by_name("erp_get_order").invoke({"order_id": "PED-78410"})  # pedido da Ana
    assert result == {"found": False}
    assert ANA not in str(result) and "189.9" not in str(result)


def test_erp_returns_own_order():
    registry, _ = _registry()
    result = registry.by_name("erp_get_order").invoke({"order_id": "PED-78231"})
    assert result["id"] == "PED-78231"


def test_erp_hides_foreign_invoice():
    registry, _ = _registry()
    result = registry.by_name("erp_get_invoice").invoke({"invoice_id": "NF-55301"})  # fatura da Ana
    assert result == {"found": False}


def test_cancel_rejects_foreign_order():
    registry, adapters = _registry()
    result = registry.by_name("erp_cancel_order").invoke({"order_id": "PED-78410", "reason": "x"})
    assert result["ok"] is False
    assert adapters.erp.get_order("PED-78410").status == "aguardando_pagamento"


def test_create_order_ignores_foreign_email():
    registry, adapters = _registry()
    before = adapters.erp.check_stock("SKU-1001").available
    result = registry.by_name("erp_create_order").invoke(
        {"email": ANA, "sku": "SKU-1001", "quantity": 1})
    assert result["ok"] is False
    assert adapters.erp.check_stock("SKU-1001").available == before


def test_forward_denied_without_allowlist():
    registry, _ = _registry()
    result = registry.by_name("email_forward").invoke({"to": "financeiro@empresa.com", "note": "cobrança"})
    assert result["ok"] is False
    assert "lista permitida" in result["error"]


def test_forward_allowed_by_domain_and_exact_address():
    assert forward_allowed("financeiro@empresa.com", parse_allowlist("@empresa.com"))
    assert forward_allowed("a@interno.com", parse_allowlist("interno.com"))
    assert forward_allowed("x@y.com", parse_allowlist("x@y.com"))
    assert not forward_allowed("x@y.com", parse_allowlist("@empresa.com"))
    assert not forward_allowed("x@y.com", set())


def test_forward_queues_when_allowlisted():
    registry, _ = _registry(allowlist="@empresa.com")
    result = registry.by_name("email_forward").invoke({"to": "financeiro@empresa.com", "note": "cobrança"})
    assert result == {"ok": True, "queued": True}
    assert registry.context.forwarded == [{"to": "financeiro@empresa.com", "note": "cobrança"}]
