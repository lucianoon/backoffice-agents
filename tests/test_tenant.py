from pathlib import Path

from conftest import ingest_only
from langchain_core.messages import AIMessage, messages_from_dict

from backoffice_agents.tenant import DEFAULT_CATEGORIES, Tenant, load_tenant

ROOT = Path(__file__).resolve().parents[1]


def test_default_toml_matches_code_defaults():
    tenant = load_tenant(ROOT / "tenants" / "default.toml")
    assert tenant.label == "default@1"
    assert tenant.categories == DEFAULT_CATEGORIES
    assert len(tenant.urgency_levels) == 5 and len(tenant.quality_levels) == 5
    assert tenant.rendered_prompt() == Tenant().rendered_prompt()


def test_missing_file_gives_defaults():
    assert load_tenant("nao/existe.toml") == Tenant()
    assert load_tenant(None).label == "default@1"


def test_custom_tenant_overrides_and_renders_prompt(tmp_path: Path):
    toml = tmp_path / "acme.toml"
    toml.write_text('''
name = "acme"
version = "3"
company = "ACME Móveis"
signature = "Time ACME"

[taxonomy.categories]
status_pedido = "where is my order"
outro = "anything else"

[prompt]
system_prompt = "Você atende {company}. Assine como {signature}."
''', encoding="utf-8")
    tenant = load_tenant(toml)
    assert tenant.label == "acme@3"
    assert list(tenant.categories) == ["status_pedido", "outro"]
    assert tenant.urgency_levels == Tenant().urgency_levels          # não sobrescrito: padrão
    assert tenant.rendered_prompt() == "Você atende ACME Móveis. Assine como Time ACME."


def test_tenant_drives_prompt_questions_and_versions(make_runner, settings, tmp_path: Path):
    toml = tmp_path / "t.toml"
    toml.write_text('name = "t"\nversion = "9"\ncompany = "Loja X"\n'
                    '[taxonomy.categories]\nstatus_pedido = "order"\nspam_irrelevante = "spam"\n',
                    encoding="utf-8")
    settings.tenant_file = str(toml)
    runner = make_runner([AIMessage(content="ok\nEquipe de Atendimento")])
    ingest_only(runner, "email:em-001")
    final = runner.process_item("email:em-001")

    assert "Loja X" in messages_from_dict(final["messages"])[0].content
    assert list(runner.jev.calls[0]["category"].criteria) == ["status_pedido", "spam_irrelevante"]
    assert {d["version"] for d in runner.store.list_decisions("email:em-001")} == {"t@9"}
    assert {c["version"] for c in runner.store.list_model_calls("email:em-001")} == {"t@9"}
