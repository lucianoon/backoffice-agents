from backoffice_agents.privacy import Pseudonymizer


def test_masks_identifiers_with_stable_tokens():
    p = Pseudonymizer()
    text = ("Contato: ana.lima@example.invalid, CPF 123.456.789-01, tel (11) 98765-4321, "
            "cartão 4111 1111 1111 1111. Repito: ana.lima@example.invalid")
    out = p.text(text)
    assert "ana.lima@example.invalid" not in out and "123.456.789-01" not in out
    assert "98765-4321" not in out and "4111 1111" not in out
    assert out.count("<email_1>") == 2          # mesmo valor, mesmo token
    assert "<cpf_1>" in out and "<telefone_1>" in out and "<cartao_1>" in out
    assert p.vault["<email_1>"] == "ana.lima@example.invalid"


def test_keeps_business_identifiers():
    p = Pseudonymizer()
    text = "Pedido PED-78231, NF-55120, rastreio BR123456789XX, total 5160.0, entrega 2026-09-22"
    assert p.text(text) == text


def test_masks_known_names_case_insensitive_and_parts():
    p = Pseudonymizer(names=["Mariana Souza"])
    out = p.text("Olá MARIANA SOUZA, a Mariana pediu; Souza confirmou. Ana não.")
    assert "Mariana" not in out and "Souza" not in out and "MARIANA" not in out
    assert "Ana" in out                          # não é parte do nome conhecido
    assert out.startswith("Olá <nome_1>")


def test_apply_recurses_into_structures():
    p = Pseudonymizer(names=["Carlos Pereira"])
    state = {"email": {"from": "carlos@example.invalid", "body": "Sou Carlos Pereira"},
             "facts": [{"tool": "erp_get_order", "result": {"customer_email": "carlos@example.invalid",
                                                             "total": 48000.0}}]}
    out = p.apply(state)
    assert out["email"]["from"] == "<email_1>"
    assert out["facts"][0]["result"]["customer_email"] == "<email_1>"
    assert out["facts"][0]["result"]["total"] == 48000.0
    assert out["email"]["body"] == "Sou <nome_1>"
