"""A suíte de contrato precisa passar nos mocks; contra sistemas reais roda por `backoffice contracts`."""

from backoffice_agents.adapters import build_adapters
from backoffice_agents.contracts import check_all, check_erp

KNOWN = dict(known_email="mariana.souza@lojaazul.com.br", known_order="PED-78231", known_sku="SKU-1001")


def test_mocks_satisfy_contracts_including_writes(settings):
    results = check_all(build_adapters(settings), allow_writes=True, **KNOWN)
    failures = {r.adapter: r.failed for r in results if r.failed}
    assert failures == {}
    assert all(not r.skipped for r in results)


def test_read_only_mode_skips_writes(settings):
    results = check_all(build_adapters(settings), allow_writes=False, **KNOWN)
    assert all(r.ok for r in results)
    assert any("escrita" in s for r in results for s in r.skipped)


def test_contract_catches_a_broken_adapter(settings):
    erp = build_adapters(settings).erp
    erp.get_order = lambda order_id: erp.orders["PED-78231"]      # devolve pedido para qualquer id
    result = check_erp(erp, "PED-78231", "SKU-1001", "mariana.souza@lojaazul.com.br")
    assert not result.ok
    assert result.failed[0][0] == "pedido desconhecido -> None"
