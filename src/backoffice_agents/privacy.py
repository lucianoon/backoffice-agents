"""Pseudonimização do estado antes de sair para o Jev (API hospedada nos EUA).

Substitui e-mails, CPF, CNPJ, cartões, telefones e os nomes conhecidos do cliente por
tokens estáveis dentro de um mesmo item (<email_1>, <nome_1>...). O Jev só devolve
decisões, então nunca é preciso reverter; o cofre fica em memória e não é persistido.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

# Ordem importa: padrões mais longos e específicos antes de telefone.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("cartao", re.compile(r"(?<!\d)(?:\d{4}[ -]?){3}\d{4}(?!\d)")),
    ("cnpj", re.compile(r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?!\d)")),
    ("cpf", re.compile(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)")),
    ("telefone", re.compile(r"(?<![\d\w])(?:\+?55\s?)?\(?\d{2}\)?\s?9?\d{4}[- ]?\d{4}(?![\d\w])")),
]


class Pseudonymizer:
    def __init__(self, names: Iterable[str] = ()) -> None:
        self._tokens: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = {}
        self.vault: dict[str, str] = {}  # token -> valor original (só em memória)
        self._name_patterns = self._compile_names(names)

    @staticmethod
    def _compile_names(names: Iterable[str]) -> list[re.Pattern[str]]:
        parts: list[str] = []
        for name in names:
            name = (name or "").strip()
            if len(name) >= 3:
                parts.append(name)
                parts.extend(p for p in name.split() if len(p) >= 3)
        # nomes completos primeiro, depois partes; sem duplicatas
        unique = list(dict.fromkeys(sorted(parts, key=len, reverse=True)))
        return [re.compile(rf"(?<!\w){re.escape(p)}(?!\w)", re.IGNORECASE) for p in unique]

    def _token(self, kind: str, value: str) -> str:
        key = (kind, value.lower())
        if key not in self._tokens:
            self._counters[kind] = self._counters.get(kind, 0) + 1
            token = f"<{kind}_{self._counters[kind]}>"
            self._tokens[key] = token
            self.vault[token] = value
        return self._tokens[key]

    def text(self, value: str) -> str:
        # identificadores estruturados primeiro, para um nome não "comer" o início de um e-mail
        for kind, pattern in PATTERNS:
            value = pattern.sub(lambda m, k=kind: self._token(k, m.group(0)), value)
        for pattern in self._name_patterns:
            value = pattern.sub(lambda m: self._token("nome", m.group(0)), value)
        return value

    def apply(self, obj: Any) -> Any:
        """Aplica recursivamente em dicts, listas e strings; outros tipos passam intactos."""
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {k: self.apply(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.apply(v) for v in obj]
        return obj
