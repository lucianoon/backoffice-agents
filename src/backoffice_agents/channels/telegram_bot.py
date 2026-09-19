"""Poller do Telegram: recebe aprovações (botões ou comandos) e retoma o item na hora.

Comandos: /pendentes, /status, /aprovar <id>, /rejeitar <id>, /item <id>
"""

from __future__ import annotations

import time

from ..runner import Runner


def _decide(runner: Runner, approval_id: int, approved: bool, who: str) -> str:
    approval = runner.store.get_approval(approval_id)
    if approval is None:
        return f"aprovação #{approval_id} não existe"
    if approval["status"] != "pending":
        return f"aprovação #{approval_id} já está {approval['status']}"
    approval = runner.store.decide_approval(approval_id, approved, who)
    try:
        final = runner.resume_item(approval)
    except Exception as exc:
        return f"#{approval_id} {'aprovada' if approved else 'rejeitada'}, mas a retomada falhou: {exc}"
    return f"#{approval_id} {'aprovada' if approved else 'rejeitada'} → item {approval['item_id']} agora {final['status']}"


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
        for item in runner.store.list_items():
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return "\n".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "Sem itens."
    if cmd in {"/aprovar", "/rejeitar"} and args and args[0].isdigit():
        return _decide(runner, int(args[0]), cmd == "/aprovar", who)
    if cmd == "/item" and args:
        item = runner.store.get_item(args[0])
        if item is None:
            return "item não encontrado"
        notes = "\n".join(item["state"].get("notes", [])) if item["state"] else ""
        return f"{item['id']} — {item['status']}\n{item['payload'].get('subject')}\n{notes}"
    return "Comandos: /pendentes, /status, /aprovar <id>, /rejeitar <id>, /item <id>"


def poll_forever(runner: Runner, sleep_s: float = 1.0) -> None:
    telegram = runner.adapters.telegram
    offset: int | None = None
    print("Telegram: aguardando comandos e aprovações (Ctrl+C para sair)")
    while True:
        updates = telegram.get_updates(offset)
        for update in updates:
            offset = update.update_id + 1
            who = f"telegram:{update.chat_id}"
            if update.callback_data:
                action, _, raw_id = update.callback_data.partition(":")
                if action in {"approve", "reject"} and raw_id.isdigit():
                    reply = _decide(runner, int(raw_id), action == "approve", who)
                    telegram.answer_callback(update.callback_id, "ok")
                    telegram.send_message(reply)
            elif update.text:
                reply = handle_text(runner, update.text, who)
                if reply:
                    telegram.send_message(reply)
        if not updates:
            time.sleep(sleep_s)
