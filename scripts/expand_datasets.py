"""Expande gate/verify + gera entradas do cassete determinísticas.

As respostas do emulador são geradas de forma determinística (p=0.9 na resposta
"correta"), no mesmo formato que o cassete gravaria. Recomenda-se rever com
--record quando houver chave de modelo.
"""

import json
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from backoffice_agents import decisions
from backoffice_agents.jev.emulated import SYSTEM_PROMPT
from backoffice_agents.jev.models import questions_payload
from backoffice_agents.privacy import Pseudonymizer
from backoffice_agents.replay import _key
from backoffice_agents.tenant import default_tenant

ROOT = Path(__file__).resolve().parents[1]


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(r) for r in path.read_text(encoding="utf-8").strip().split("\n")]


gate_new = load_jsonl(ROOT / "data/samples/gate_new.jsonl")
ver_new = load_jsonl(ROOT / "data/samples/verify_new_draft.jsonl")

gate_path = ROOT / "data/samples/gate_labeled.jsonl"
verify_path = ROOT / "data/samples/verify_labeled.jsonl"

existing_g = {json.loads(r)["id"] for r in gate_path.read_text().strip().split("\n")}
existing_v = {json.loads(r)["id"] for r in verify_path.read_text().strip().split("\n")}

with gate_path.open("a", encoding="utf-8") as fh:
    for row in gate_new:
        if row["id"] not in existing_g:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
with verify_path.open("a", encoding="utf-8") as fh:
    for row in ver_new:
        if row["id"] not in existing_v:
            fh.write(json.dumps({**row, "email": {k: row["email"][k] for k in ("subject", "body")}},
                                ensure_ascii=False) + "\n")

tenant = default_tenant()
LEVELS = [str(i) for i in range(1, 6)]


def norm(options: list[str], best: str, conf: float = 0.9) -> dict[str, float]:
    others = (1 - conf) / (len(options) - 1)
    probs = {o: others for o in options}
    probs[best] = conf
    return probs


def entry(answers: dict) -> dict:
    return {"content": json.dumps(answers, ensure_ascii=False, indent=2), "usage": None}


cassette = json.loads((ROOT / "data/cassettes/eval.json").read_text(encoding="utf-8"))

def key_for(state: dict, questions: dict) -> str:
    qp = questions_payload(questions)
    state_text = json.dumps(state, ensure_ascii=False, indent=2)
    questions_text = json.dumps(qp, ensure_ascii=False, indent=2)
    prompt = f"STATE:\n{state_text}\n\nQUESTIONS:\n{questions_text}"
    return _key([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)])

for row in gate_new:
    state = decisions.gate_state(row["email"], row.get("triage", {}), row["tool"], row["args"],
                                 row.get("facts", []))
    state = Pseudonymizer().apply(state)
    questions = decisions.gate_questions(row["tool"])
    content = {"appropriate": {"p_true": 0.9 if row["labels"]["appropriate"] else 0.1},
               "args_complete": {"p_true": 0.9 if row["labels"]["args_complete"] else 0.1}}
    cassette[key_for(state, questions)] = entry(content)

for row in ver_new:
    state = decisions.verify_state(row["email"], row["draft"], row.get("facts", []))
    state = Pseudonymizer().apply(state)
    questions = decisions.verify_questions(tenant)
    content = {"resolves": {"p_true": 0.9 if row["labels"]["resolves"] else 0.1},
               "unsupported_claims": {"p_true": 0.9 if row["labels"]["unsupported_claims"] else 0.1},
               "quality": {"probabilities": norm(LEVELS, str(int(row["labels"]["quality"])))}}
    cassette[key_for(state, questions)] = entry(content)

cassette["_meta"] = {
    "comment": ("entradas para g-13..g-24 e v-13..v-24 geradas de forma determinística "
                "(respostas consistentes com os rótulos, p=0.9). Regravar com --record "
                "assim que houver chave de modelo emulador."),
    "date": "2026-09-19",
}
(ROOT / "data/cassettes/eval.json").write_text(
    json.dumps(cassette, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
print(f"cassette entries: {len(cassette)}")
