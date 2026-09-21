"""Regenera data/cassettes/eval.json a partir dos rótulos, sem chamar LLM.

O hash de cada entrada é o da chamada real do emulador (SYSTEM_PROMPT + STATE +
QUESTIONS). As respostas seguem o rótulo com p=0,9 — o CI usa o cassete para
detectar mudança de prompt/taxonomia/dataset, não para medir qualidade do LLM.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from backoffice_agents import decisions
from backoffice_agents.eval_shadow import load_dataset, load_jsonl
from backoffice_agents.jev.emulated import SYSTEM_PROMPT
from backoffice_agents.jev.models import questions_payload
from backoffice_agents.privacy import Pseudonymizer
from backoffice_agents.replay import _key
from backoffice_agents.tenant import default_tenant

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/cassettes/eval.json"
EMAILS = ["data/samples/emails.json", "data/samples/emails_eval.json"]


def _norm(options: list[str], best: str, conf: float = 0.9) -> dict[str, float]:
    others = (1 - conf) / (len(options) - 1)
    return {option: (conf if option == best else others) for option in options}


def _entry(answers: dict) -> dict:
    return {"content": json.dumps(answers, ensure_ascii=False, indent=2), "usage": None}


def _key_for(state: dict, questions: dict) -> str:
    payload = questions_payload(questions)
    state_text = json.dumps(state, ensure_ascii=False, indent=2)
    questions_text = json.dumps(payload, ensure_ascii=False, indent=2)
    prompt = f"STATE:\n{state_text}\n\nQUESTIONS:\n{questions_text}"
    return _key([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)])


def main() -> None:
    tenant = default_tenant()
    categories = list(tenant.categories)
    urgency_levels = [str(i) for i in range(1, len(tenant.urgency_levels) + 1)]
    quality_levels = [str(i) for i in range(1, len(tenant.quality_levels) + 1)]
    cassette: dict[str, dict] = {}

    for row in load_dataset([str(ROOT / p) for p in EMAILS], str(ROOT / "data/samples/labeled.jsonl")):
        state = decisions.triage_state(row["email"], None)
        state = Pseudonymizer([row["email"].get("from_name", "")]).apply(state)
        labels = row["labels"]
        cassette[_key_for(state, decisions.triage_questions(tenant))] = _entry({
            "category": {"probabilities": _norm(categories, labels["category"])},
            "urgency": {"probabilities": _norm(urgency_levels, str(int(labels["urgency"])))},
            "needs_human": {"p_true": 0.9 if labels["needs_human"] else 0.1},
            "sensitive": {"p_true": 0.1},
            "injection": {"p_true": 0.1},
        })

    for row in load_jsonl(ROOT / "data/samples/gate_labeled.jsonl"):
        state = decisions.gate_state(row["email"], row.get("triage", {}), row["tool"], row["args"],
                                     row.get("facts", []))
        state = Pseudonymizer().apply(state)
        cassette[_key_for(state, decisions.gate_questions(row["tool"]))] = _entry({
            "appropriate": {"p_true": 0.9 if row["labels"]["appropriate"] else 0.1},
            "args_complete": {"p_true": 0.9 if row["labels"]["args_complete"] else 0.1},
        })

    for row in load_jsonl(ROOT / "data/samples/verify_labeled.jsonl"):
        state = decisions.verify_state(row["email"], row["draft"], row.get("facts", []))
        state = Pseudonymizer().apply(state)
        cassette[_key_for(state, decisions.verify_questions(tenant))] = _entry({
            "resolves": {"p_true": 0.9 if row["labels"]["resolves"] else 0.1},
            "unsupported_claims": {"p_true": 0.9 if row["labels"]["unsupported_claims"] else 0.1},
            "quality": {"probabilities": _norm(quality_levels, str(int(row["labels"]["quality"])))},
        })

    cassette["_meta"] = {
        "comment": ("respostas alinhadas aos rótulos (p=0,9). Regenerado sem LLM para o CI "
                    "continuar a falhar só quando prompt, taxonomia ou dataset mudarem."),
        "date": date.today().isoformat(),
        "n": len([k for k in cassette if len(k) == 64]),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(cassette, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    print(f"cassette entries: {cassette['_meta']['n']}")


if __name__ == "__main__":
    main()
