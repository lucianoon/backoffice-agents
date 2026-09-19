"""Perguntas tipadas que o sistema faz ao Jev em cada etapa.

Instruções em inglês (idioma primário do Jev); o `state` vai em português, que é o
que o piloto precisa medir. Cada pergunta é um "gut check" sobre uma coisa só.
"""

from __future__ import annotations

from typing import Any

from .jev import ChoiceQuestion, NoulQuestion, Question, ScoreQuestion

CATEGORIES: dict[str, str] = {
    "status_pedido": "customer asks where an order is, tracking or delivery date",
    "financeiro_cobranca": "invoices, boleto, payment, refund of an amount already charged",
    "comercial_vendas": "quote, price, discount, availability for a possible purchase",
    "cancelamento": "customer wants to cancel an order or contract",
    "suporte_tecnico": "product defect, damage, warranty, exchange",
    "reclamacao": "complaint about service, threat of Procon/legal action, repeated contact",
    "spam_irrelevante": "marketing, phishing or unrelated to the company",
    "outro": "none of the above",
}

URGENCY_LEVELS = [
    "no urgency: informational, can wait days",
    "low: routine request, answer within 2 business days",
    "medium: customer expects an answer today",
    "high: deadline within 24h or money at risk",
    "critical: legal threat, repeated ignored contact or service outage",
]

QUALITY_LEVELS = [
    "unacceptable: wrong, rude or ignores the question",
    "poor: partially answers, vague or confusing",
    "acceptable: answers the question but could be clearer",
    "good: clear, complete, polite",
    "excellent: clear, complete, polite, anticipates next step",
]


def triage_questions() -> dict[str, Question]:
    return {
        "category": ChoiceQuestion(instructions="What is the main intent of this customer email?",
                                   criteria=dict(CATEGORIES)),
        "urgency": ScoreQuestion(instructions="How urgent is this email?", criteria=URGENCY_LEVELS),
        "needs_human": NoulQuestion(
            instructions="Does this email require a human agent rather than an automated assistant?",
            criteria={"true": "anger, legal threat, ambiguity, negotiation, irreversible request",
                      "false": "routine lookup or standard reply is enough"}),
        "sensitive": NoulQuestion(
            instructions="Does the email contain sensitive personal data (CPF, card number, health, "
                         "banking details) beyond name and email?"),
    }


def triage_state(email: dict[str, Any], contact: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "email": {k: email.get(k, "") for k in ("from_addr", "from_name", "subject", "body", "date")},
        "crm_contact": contact or {"found": False},
    }


def gate_questions(tool_name: str) -> dict[str, Question]:
    return {
        "appropriate": NoulQuestion(
            instructions=f"Is calling the tool `{tool_name}` an appropriate next step for the task?"),
        "args_complete": NoulQuestion(
            instructions="Are the proposed arguments complete, correct and consistent with the "
                         "customer's request and the data gathered so far?"),
    }


def gate_state(email: dict[str, Any], triage: dict[str, Any], tool_name: str, args: dict[str, Any],
               facts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "customer_request": {"subject": email.get("subject", ""), "body": email.get("body", "")},
        "triage": triage,
        "proposed_tool_call": {"tool": tool_name, "arguments": args},
        "data_gathered_so_far": facts,
    }


def verify_questions() -> dict[str, Question]:
    return {
        "resolves": NoulQuestion(
            instructions="Does the draft reply actually answer what the customer asked?"),
        "quality": ScoreQuestion(instructions="Rate the draft reply quality.", criteria=QUALITY_LEVELS),
        "unsupported_claims": NoulQuestion(
            instructions="Does the draft state facts (dates, amounts, codes, promises) that are NOT "
                         "supported by the data gathered?"),
    }


def verify_state(email: dict[str, Any], draft: str, facts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "customer_request": {"subject": email.get("subject", ""), "body": email.get("body", "")},
        "data_gathered": facts,
        "draft_reply": draft,
    }
