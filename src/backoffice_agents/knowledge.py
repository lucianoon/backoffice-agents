"""Base de conhecimento de políticas: recuperação lexical + Jev pontuando a relevância dos trechos.

Fluxo: os documentos em `KB_DIR` são divididos em trechos por título; uma busca lexical barata
seleciona candidatos; o Jev responde, em UMA chamada, um Score de relevância por candidato
(perguntas paralelas), e só os trechos relevantes vão ao LLM. É o uso de "RAG passage
classification" que a TypeSafe documenta.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .jev import JevClient, JevResponse, ScoreQuestion
from .privacy import Pseudonymizer

RELEVANCE_LEVELS = [
    "irrelevant: does not help answer the question",
    "tangential: same topic but does not answer it",
    "relevant: partially answers the question",
    "essential: directly answers the question",
]
STOPWORDS = {"a", "o", "e", "de", "da", "do", "das", "dos", "em", "um", "uma", "para", "com", "que",
             "por", "na", "no", "os", "as", "se", "ao", "à", "é", "ou", "the", "of", "to", "and"}


@dataclass
class Passage:
    source: str
    heading: str
    text: str

    @property
    def ref(self) -> str:
        return f"{self.source} > {self.heading}"


@dataclass
class KbHit:
    passage: Passage
    score: float
    confidence: float


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _terms(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", _normalize(text)) if t not in STOPWORDS}


def load_passages(kb_dir: str | Path) -> list[Passage]:
    """Cada seção `##` de cada .md vira um trecho; o texto antes do primeiro `##` usa o título `#`."""
    passages: list[Passage] = []
    for path in sorted(Path(kb_dir).glob("*.md")):
        heading = path.stem
        buffer: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                if buffer and "".join(buffer).strip():
                    passages.append(Passage(path.name, heading, "\n".join(buffer).strip()))
                buffer = []
                heading = line.lstrip("#").strip()
            else:
                buffer.append(line)
        if buffer and "".join(buffer).strip():
            passages.append(Passage(path.name, heading, "\n".join(buffer).strip()))
    return passages


class KnowledgeBase:
    def __init__(self, kb_dir: str | Path, jev: JevClient, candidates: int = 8, top_k: int = 3,
                 min_score: float = 2.5, anonymize: bool = True) -> None:
        self.passages = load_passages(kb_dir)
        self._jev = jev
        self._candidates = candidates
        self._top_k = top_k
        self._min_score = min_score
        self._anonymize = anonymize  # a pergunta vem do LLM e pode carregar dados do cliente

    def lexical_candidates(self, query: str) -> list[Passage]:
        terms = _terms(query)
        scored = []
        for passage in self.passages:
            hay = _terms(passage.heading + " " + passage.text)
            overlap = len(terms & hay)
            if overlap:
                scored.append((overlap + 0.5 * len(terms & _terms(passage.heading)), passage))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [p for _, p in scored[: self._candidates]]

    def search(self, query: str, on_jev: Callable[[str, JevResponse], None] | None = None) -> list[KbHit]:
        candidates = self.lexical_candidates(query)
        if not candidates:
            return []
        questions = {
            f"p{i}": ScoreQuestion(
                instructions=f"How relevant is passage p{i} for answering the customer question?",
                criteria=RELEVANCE_LEVELS)
            for i in range(len(candidates))
        }
        state = {"customer_question": Pseudonymizer().text(query) if self._anonymize else query,
                 "passages": {f"p{i}": {"ref": p.ref, "text": p.text} for i, p in enumerate(candidates)}}
        response = self._jev.ask(state, questions)
        if on_jev:
            on_jev("kb_search", response)
        hits = []
        for i, passage in enumerate(candidates):
            answer = response.score(f"p{i}")
            if answer.score >= self._min_score:
                hits.append(KbHit(passage, answer.score, answer.confidence))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[: self._top_k]

    @staticmethod
    def to_tool_result(hits: list[KbHit]) -> dict[str, Any]:
        if not hits:
            return {"found": False,
                    "hint": "nenhuma política relevante; não invente regra, escale se preciso"}
        return {"found": True, "passages": [
            {"ref": h.passage.ref, "relevance": round(h.score, 2), "text": h.passage.text} for h in hits]}
