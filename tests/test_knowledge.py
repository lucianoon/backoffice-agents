"""Base de conhecimento: trechos, busca lexical, pontuação pelo Jev e uso como ferramenta."""

from pathlib import Path

from conftest import FakeJev, ingest_only, tool_call
from langchain_core.messages import AIMessage

from backoffice_agents.knowledge import KnowledgeBase, load_passages

KB = Path(__file__).resolve().parents[1] / "data" / "kb"


def test_passages_split_by_heading():
    passages = load_passages(KB)
    refs = {p.ref for p in passages}
    assert "garantia.md > Prazos de garantia" in refs
    assert all(p.text for p in passages) and len(passages) >= 12


def test_lexical_candidates_prefer_matching_headings():
    kb = KnowledgeBase(KB, FakeJev(), candidates=3)
    candidates = kb.lexical_candidates("qual o prazo de garantia do pistão da cadeira?")
    assert candidates and candidates[0].source == "garantia.md"


def test_search_keeps_only_passages_jev_marks_relevant():
    # p0 essencial, p1 tangencial, resto irrelevante
    jev = FakeJev({"p0": 3.8, "p1": 1.9, "_score_default": 1.0})
    kb = KnowledgeBase(KB, jev, candidates=4, top_k=3, min_score=2.5)
    query = "prazo de reembolso e estorno após cancelamento do pedido"
    candidates = kb.lexical_candidates(query)
    assert 2 <= len(candidates) <= 4
    logged = []
    hits = kb.search(query, on_jev=lambda s, r: logged.append(s))
    assert len(hits) == 1 and hits[0].score == 3.8 and hits[0].passage is candidates[0]
    assert logged == ["kb_search"]
    assert len(jev.calls[0]) == len(candidates)         # um Score por candidato, numa só chamada
    assert KnowledgeBase.to_tool_result([])["found"] is False


def test_search_query_is_pseudonymized_before_jev():
    jev = FakeJev()
    KnowledgeBase(KB, jev, anonymize=True).search(
        "prazo de troca para ana.lima@example.invalid CPF 123.456.789-01"
    )
    assert "ana.lima@example.invalid" not in jev.states[0]["customer_question"]
    assert "<email_1>" in jev.states[0]["customer_question"]


def test_agent_can_call_kb_search_tool_and_it_is_logged(make_runner):
    runner = make_runner([
        tool_call("kb_search", {"query": "prazo para arrependimento e reembolso"}, "c1"),
        AIMessage(content="Você pode desistir em até 7 dias corridos.\nEquipe de Atendimento"),
    ], jev_overrides={"_score_default": 3.5})
    ingest_only(runner, "email:em-001")
    final = runner.process_item("email:em-001")
    assert final["status"] == "sent"
    stages = [c["stage"] for c in runner.store.list_model_calls("email:em-001") if c["kind"] == "jev"]
    assert stages == ["triage", "kb_search", "verify"]
    facts = [f for f in runner.jev.states[-1]["data_gathered"] if f["tool"] == "kb_search"]
    assert facts and facts[0]["result"]["found"] is True
    assert any("Arrependimento" in p["ref"] for p in facts[0]["result"]["passages"])
