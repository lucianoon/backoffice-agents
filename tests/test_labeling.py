import json
from pathlib import Path

from backoffice_agents.labeling import agreement, cohen_kappa, dump_mailbox, export_batch, load_labels, merge

A = {
    "e1": {"category": "status_pedido", "urgency": 2, "needs_human": False},
    "e2": {"category": "cancelamento", "urgency": 4, "needs_human": True},
    "e3": {"category": "suporte_tecnico", "urgency": 3, "needs_human": False},
    "e4": {"category": "outro", "urgency": 1, "needs_human": False},
}
B = {
    "e1": {"category": "status_pedido", "urgency": 3, "needs_human": False},   # urgência difere em 1
    "e2": {"category": "cancelamento", "urgency": 4, "needs_human": True},
    "e3": {"category": "reclamacao_atendimento", "urgency": 3, "needs_human": True},  # dois campos
    "e5": {"category": "outro", "urgency": 1, "needs_human": False},           # só em B
}


def test_kappa_bounds():
    assert cohen_kappa(["a", "b", "a", "b"], ["a", "b", "a", "b"]) == 1.0
    assert cohen_kappa(["a", "a", "a"], ["a", "a", "a"]) == 1.0
    assert cohen_kappa(["a", "b"], ["b", "a"]) < 0
    assert cohen_kappa([], []) == 0.0


def test_agreement_report():
    report = agreement(A, B)
    by_field = {f.field: f for f in report.fields}
    assert by_field["category"].n == 3 and abs(by_field["category"].exact - 2 / 3) < 1e-9
    assert by_field["urgency"].within_one == 1.0 and abs(by_field["urgency"].exact - 2 / 3) < 1e-9
    assert {(d["id"], d["field"]) for d in report.disagreements} == {
        ("e1", "urgency"), ("e3", "category"), ("e3", "needs_human")}
    assert report.only_in_a == ["e4"] and report.only_in_b == ["e5"]


def test_merge_keeps_agreed_and_isolates_conflicts():
    merged, conflicts = merge(A, B)
    assert [m["id"] for m in merged] == ["e2"]
    assert {c["id"] for c in conflicts} == {"e1", "e3"}
    e3 = next(c for c in conflicts if c["id"] == "e3")
    assert set(e3["conflicts"]) == {"category", "needs_human"} and e3["agreed"] == {"urgency": 3}


def test_export_and_load_roundtrip(tmp_path: Path):
    out = tmp_path / "lote.jsonl"
    n = export_batch([{"id": "x1", "subject": "s", "body": "b", "from_addr": "a@b.c"}], out)
    assert n == 1
    row = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert row["labels"] == {"category": None, "urgency": None, "needs_human": None}
    row["labels"] = {"category": "outro", "urgency": "2", "needs_human": "sim"}
    out.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert load_labels(out)["x1"] == {"category": "outro", "urgency": 2, "needs_human": True}


def test_dump_mailbox_writes_emails_and_empty_labels(tmp_path: Path):
    emails = tmp_path / "box.json"
    labels = tmp_path / "lote.jsonl"
    n = dump_mailbox([{"id": "m1", "from_addr": "a@b.c", "subject": "s", "body": "oi",
                       "attachments": [{"filename": "x.pdf", "data_b64": "AAAA", "size": 3}]}],
                     emails, labels)
    assert n == 1
    saved = json.loads(emails.read_text())[0]
    assert saved["id"] == "m1"
    assert "data_b64" not in saved["attachments"][0]
    assert saved["attachments"][0]["filename"] == "x.pdf"
    assert json.loads(labels.read_text().splitlines()[0])["labels"]["category"] is None
