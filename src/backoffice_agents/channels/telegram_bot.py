"""Poller do Telegram: aprovações (botões ou comandos), rótulos humanos e relatórios.

Comandos: /pendentes, /status, /aprovar <id>, /rejeitar <id>, /item <id>, /calibracao
Botões: approve:<id> | reject:<id> | lbl:ok:<item> | lbl:fix:<item> | lbl:set:<item>:<categoria>
"""

from __future__ import annotations

import time

from ..adapters.telegram import Button
from ..calibration import calibration_report, format_report
from ..runner import Runner

Reply = tuple[str, list[Button] | None]


def _decide(runner: Runner, approval_id: int, approved: bool, who: str) -> str:
    approval = runner.store.get_approval(approval_id)
    if approval is None:
        return f"aprovação #{approval_id} não existe"
    if approval["status"] != "pending":
        return f"aprovação #{approval_id} já está {approval['status']}"
    approval = runner.store.decide_approval(approval_id, approved, who)
    if approval is None:
        return f"aprovação #{approval_id} não existe"
    try:
        final = runner.resume_item(approval)
    except Exception as exc:
        return f"#{approval_id} {'aprovada' if approved else 'rejeitada'}, mas a retomada falhou: {exc}"
    verb = "aprovada" if approved else "rejeitada"
    return f"#{approval_id} {verb} → item {approval['item_id']} agora {final['status']}"


def _label_category(runner: Runner, item_id: str, category: str | None, who: str) -> str:
    """Grava o rótulo humano da categoria e quem rotulou. `None` = confirma a categoria prevista."""
    item = runner.store.get_item(item_id)
    if item is None:
        return f"item {item_id} não existe"
    predicted = (item.get("state") or {}).get("triage", {}).get("category")
    label = category or predicted
    if not label:
        return f"item {item_id} não tem triagem para rotular"
    n = runner.store.set_human_label(item_id, "triage", "category", label, labeled_by=who)
    if n == 0:
        return f"item {item_id} não tem decisão de triagem registrada"
    verdict = "confirmada" if label == predicted else f"corrigida ({predicted} → {label})"
    return f"📝 Categoria {verdict} para {item_id}. Obrigado!"


def _categories(runner: Runner) -> list[str]:
    return list(runner.tenant.categories) if runner.tenant else []


def handle_callback(runner: Runner, data: str, who: str) -> Reply:
    parts = data.split(":")
    action = parts[0]
    if action in {"approve", "reject"} and len(parts) == 2 and parts[1].isdigit():
        return _decide(runner, int(parts[1]), action == "approve", who), None
    if action == "lbl" and len(parts) >= 3:
        sub, item_id = parts[1], ":".join(parts[2:])
        if sub == "ok":
            return _label_category(runner, item_id, None, who), None
        if sub == "fix":
            buttons = [Button(text=cat, callback_data=f"lbl:set:{item_id}:{cat}")
                       for cat in _categories(runner)]
            return f"Qual é a categoria correta de {item_id}?", buttons
        if sub == "set":
            item_id, _, category = item_id.rpartition(":")
            if category in _categories(runner):
                return _label_category(runner, item_id, category, who), None
    return "botão desconhecido", None


def handle_text(runner: Runner, text: str, who: str) -> str:
    parts = text.strip().split()
    if not parts:
        return ""
    cmd, args = parts[0].lower(), parts[1:]
    if cmd == "/pendentes":
        pending = runner.store.list_approvals("pending")
        if not pending:
            return "Nenhuma aprovação pendente."
        return "\n".join(f"#{a['id']} [{a['kind']}] item {a['item_id']}: "
                         f"{a['action'].get('tool') or 'envio de resposta'}" for a in pending)
    if cmd == "/status":
        counts: dict[str, int] = {}
        for listed in runner.store.list_items():
            counts[listed["status"]] = counts.get(listed["status"], 0) + 1
        return "\n".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "Sem itens."
    if cmd in {"/aprovar", "/rejeitar"} and args and args[0].isdigit():
        return _decide(runner, int(args[0]), cmd == "/aprovar", who)
    if cmd == "/item" and args:
        item = runner.store.get_item(args[0])
        if item is None:
            return "item não encontrado"
        notes = "\n".join(item["state"].get("notes", [])) if item["state"] else ""
        return f"{item['id']} — {item['status']}\n{item['payload'].get('subject')}\n{notes}"
    if cmd == "/calibracao":
        return "📊 Calibração (decisões com rótulo humano)\n" + format_report(
            calibration_report(runner.store.list_decisions()))
    return ("Comandos: /pendentes, /status, /aprovar <id>, /rejeitar <id>, /item <id>, /calibracao")


def poll_forever(runner: Runner, sleep_s: float = 1.0) -> None:
    telegram = runner.adapters.telegram
    offset: int | None = None
    print("Telegram: aguardando comandos, aprovações e rótulos (Ctrl+C para sair)")
    while True:
        updates = telegram.get_updates(offset)
        for update in updates:
            offset = update.update_id + 1
            who = f"telegram:{update.user_id or update.chat_id}"
            if update.callback_data:
                text, buttons = handle_callback(runner, update.callback_data, who)
                telegram.answer_callback(update.callback_id, "ok")
                telegram.send_message(text, buttons)
            elif update.text:
                reply = handle_text(runner, update.text, who)
                if reply:
                    telegram.send_message(reply)
        if not updates:
            time.sleep(sleep_s)
