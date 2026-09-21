import pytest

from backoffice_agents.adapters import build_adapters, load_adapter_class
from backoffice_agents.adapters.crm import MockCrmAdapter
from backoffice_agents.adapters.email import MockEmailAdapter
from backoffice_agents.adapters.erp import MockErpAdapter


def test_mock_adapters_are_the_default(settings):
    adapters = build_adapters(settings)
    assert isinstance(adapters.crm, MockCrmAdapter)
    assert isinstance(adapters.erp, MockErpAdapter)


def test_crm_and_erp_load_from_import_path(settings):
    settings.crm_adapter = "backoffice_agents.adapters.crm:MockCrmAdapter"
    settings.erp_adapter = "backoffice_agents.adapters.erp:MockErpAdapter"
    adapters = build_adapters(settings)
    assert isinstance(adapters.crm, MockCrmAdapter)
    assert adapters.crm.find_contact_by_email("mariana.souza@lojaazul.com.br") is not None
    assert adapters.erp.get_order("PED-78231") is not None


def test_mock_fetch_recent_takes_the_last_n(tmp_path):
    path = tmp_path / "box.json"
    path.write_text(
        '[{"id":"a","from_addr":"a@x.com","subject":"1","body":"a"},'
        '{"id":"b","from_addr":"b@x.com","subject":"2","body":"b"},'
        '{"id":"c","from_addr":"c@x.com","subject":"3","body":"c"}]',
        encoding="utf-8")
    adapter = MockEmailAdapter(str(path))
    assert [m.id for m in adapter.fetch_recent(2)] == ["b", "c"]
    adapter.mark_processed("c")
    assert [m.id for m in adapter.fetch_unread()] == ["a", "b"]
    assert [m.id for m in adapter.fetch_recent(2)] == ["b", "c"]


def test_invalid_adapter_spec_explains_the_format():
    with pytest.raises(ValueError, match="modulo:Classe"):
        load_adapter_class("sem_dois_pontos")
