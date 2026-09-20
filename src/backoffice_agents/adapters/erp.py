from __future__ import annotations

import itertools
from typing import Protocol

from pydantic import BaseModel


class OrderItem(BaseModel):
    sku: str
    description: str
    quantity: int
    unit_price: float


class Order(BaseModel):
    id: str
    customer_email: str
    status: str  # aguardando_pagamento | em_separacao | enviado | entregue | cancelado
    items: list[OrderItem]
    total: float
    created_at: str
    tracking_code: str = ""
    expected_delivery: str = ""
    delivered_at: str = ""


class Invoice(BaseModel):
    id: str
    order_id: str
    customer_email: str
    amount: float
    due_date: str
    status: str  # aberta | paga | vencida
    boleto_url: str = ""


class StockLevel(BaseModel):
    sku: str
    description: str
    available: int
    unit_price: float


class ErpAdapter(Protocol):
    def get_order(self, order_id: str) -> Order | None: ...
    def list_orders(self, customer_email: str) -> list[Order]: ...
    def get_invoice(self, invoice_id: str) -> Invoice | None: ...
    def list_invoices(self, customer_email: str) -> list[Invoice]: ...
    def check_stock(self, sku: str) -> StockLevel | None: ...
    def create_order(self, customer_email: str, sku: str, quantity: int) -> Order: ...
    def cancel_order(self, order_id: str, reason: str) -> Order: ...


class MockErpAdapter:
    def __init__(self) -> None:
        self.stock: dict[str, StockLevel] = {
            "SKU-1001": StockLevel(sku="SKU-1001", description="Cadeira ergonômica Pro", available=12,
                                   unit_price=1290.0),
            "SKU-2002": StockLevel(sku="SKU-2002", description="Mesa regulável 140cm", available=15,
                                   unit_price=2490.0),
            "SKU-3003": StockLevel(sku="SKU-3003", description="Luminária LED articulada", available=57,
                                   unit_price=189.9),
        }
        self.orders: dict[str, Order] = {
            "PED-78231": Order(
                id="PED-78231", customer_email="mariana.souza@lojaazul.com.br", status="enviado",
                items=[OrderItem(sku="SKU-1001", description="Cadeira ergonômica Pro", quantity=4,
                                 unit_price=1290.0)],
                total=5160.0, created_at="2026-09-10", tracking_code="BR123456789XX",
                expected_delivery="2026-09-22",
            ),
            "PED-78410": Order(
                id="PED-78410", customer_email="ana.lima@gmail.com", status="aguardando_pagamento",
                items=[OrderItem(sku="SKU-3003", description="Luminária LED articulada", quantity=1,
                                 unit_price=189.9)],
                total=189.9, created_at="2026-09-17",
            ),
            # piloto: casos do lote de e-mails reais de teste (remetente = caixa do piloto)
            "PED-79450": Order(
                id="PED-79450", customer_email="prradical@gmail.com", status="em_separacao",
                items=[OrderItem(sku="SKU-3003", description="Luminária LED articulada", quantity=1,
                                 unit_price=189.9)],
                total=189.9, created_at="2026-09-18",
            ),
            "PED-80011": Order(
                id="PED-80011", customer_email="prradical@gmail.com", status="entregue",
                items=[OrderItem(sku="SKU-3003", description="Luminária LED articulada branco",
                                 quantity=1, unit_price=189.9)],
                total=189.9, created_at="2026-09-10", tracking_code="BR987654321XX",
                delivered_at="2026-09-15",
            ),
        }
        self.invoices: dict[str, Invoice] = {
            "NF-55120": Invoice(id="NF-55120", order_id="PED-78231",
                                customer_email="mariana.souza@lojaazul.com.br", amount=5160.0,
                                due_date="2026-09-25", status="aberta",
                                boleto_url="https://erp.exemplo.com/boleto/NF-55120"),
            "NF-55301": Invoice(id="NF-55301", order_id="PED-78410", customer_email="ana.lima@gmail.com",
                                amount=189.9, due_date="2026-09-20", status="aberta",
                                boleto_url="https://erp.exemplo.com/boleto/NF-55301"),
            # piloto: casos do lote de e-mails reais de teste
            "NF-55201": Invoice(id="NF-55201", order_id="PED-79450", customer_email="prradical@gmail.com",
                                amount=189.9, due_date="2026-09-20", status="aberta",
                                boleto_url="https://erp.exemplo.com/boleto/NF-55201"),
            "NF-55210": Invoice(id="NF-55210", order_id="PED-80011", customer_email="prradical@gmail.com",
                                amount=189.9, due_date="2026-09-08", status="paga",
                                boleto_url="https://erp.exemplo.com/boleto/NF-55210"),
            "NF-55215": Invoice(id="NF-55215", order_id="PED-78231", customer_email="prradical@gmail.com",
                                amount=5160.0, due_date="2026-09-25", status="paga",
                                boleto_url="https://erp.exemplo.com/boleto/NF-55215"),
        }
        self._ids = itertools.count(90000)

    def get_order(self, order_id: str) -> Order | None:
        return self.orders.get(order_id.strip().upper())

    def list_orders(self, customer_email: str) -> list[Order]:
        email = customer_email.strip().lower()
        return [o for o in self.orders.values() if o.customer_email.lower() == email]

    def get_invoice(self, invoice_id: str) -> Invoice | None:
        return self.invoices.get(invoice_id.strip().upper())

    def list_invoices(self, customer_email: str) -> list[Invoice]:
        email = customer_email.strip().lower()
        return [i for i in self.invoices.values() if i.customer_email.lower() == email]

    def check_stock(self, sku: str) -> StockLevel | None:
        return self.stock.get(sku.strip().upper())

    def create_order(self, customer_email: str, sku: str, quantity: int) -> Order:
        level = self.check_stock(sku)
        if level is None:
            raise ValueError(f"SKU inexistente: {sku}")
        if level.available < quantity:
            raise ValueError(f"estoque insuficiente para {sku}: {level.available} disponível")
        level.available -= quantity
        order = Order(
            id=f"PED-{next(self._ids)}", customer_email=customer_email, status="aguardando_pagamento",
            items=[OrderItem(sku=level.sku, description=level.description, quantity=quantity,
                             unit_price=level.unit_price)],
            total=round(level.unit_price * quantity, 2), created_at="2026-09-19",
        )
        self.orders[order.id] = order
        return order

    def cancel_order(self, order_id: str, reason: str) -> Order:
        order = self.get_order(order_id)
        if order is None:
            raise ValueError(f"pedido inexistente: {order_id}")
        if order.status in {"enviado", "entregue"}:
            raise ValueError(f"pedido {order.id} já {order.status}; cancelamento exige devolução")
        order.status = "cancelado"
        return order
