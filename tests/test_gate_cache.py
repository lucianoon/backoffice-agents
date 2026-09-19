"""Cache de gate, staleness do cassete e deduplicação de consultas ao Jev."""

from conftest import FakeJev, tool_call
from langchain_core.messages import AIMessage, HumanMessage


def test_gate_cache_avoids_duplicate_jev_call(make_runner):
    """Duas chamadas idênticas no mesmo passo consultam o gate uma vez só."""
    reply = "Olá Mariana, seu pedido PED-78231 foi enviado.\nEquipe de Atendimento"
    call = {"name": "crm_log_interaction",
            "args": {"email": "mariana.souza@lojaazul.com.br",
                     "summary": "Cliente pediu rastreio; informado."},
            "id": "c2a", "type": "tool_call"}
    jev = FakeJev()
    runner = make_runner([
        tool_call("erp_get_order", {"order_id": "PED-78231"}, "c1"),
        AIMessage(content="", tool_calls=[call, dict(call, id="c2b")]),
        AIMessage(content=reply),
    ], jev=jev)
    runner.ingest_emails()
    final = runner.process_item("email:em-001")

    assert final["status"] == "sent"
    gate_calls = [q for q in jev.calls if "appropriate" in q]
    assert len(gate_calls) == 1        # segunda proposta idêntica veio do cache
    stages = [d["stage"] for d in runner.store.list_decisions("email:em-001")]
    assert stages.count("gate:crm_log_interaction") == 2   # gate: 2 perguntas, 1 chamada


def test_replay_unused_keys_reports_stale(tmp_path):
    from backoffice_agents.replay import ReplayChatModel, ReplayMiss

    class Inner:
        def invoke(self, messages):
            return AIMessage(content="x")

    cassette = tmp_path / "c.json"
    recorder = ReplayChatModel(cassette_path=str(cassette), mode="record", inner=Inner())
    recorder.invoke([HumanMessage(content="olá")])
    recorder.entries["_meta"] = {"comment": "não é hash"}

    player = ReplayChatModel(cassette_path=str(cassette), mode="replay")
    assert len(player.unused_keys) == 1          # '_meta' não conta (não é sha256)
    try:
        player.invoke([HumanMessage(content="olá diferente")])
    except ReplayMiss:
        pass
    player.invoke([HumanMessage(content="olá")])
    assert player.unused_keys == []
