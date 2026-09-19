"""Telegram como canal do operador: notificações, aprovações com botões e comandos.

Segurança: só o chat autorizado é aceito e, quando `operators` está definido, só os usuários
listados podem aprovar, rejeitar ou rotular. O id do usuário fica registrado em quem decidiu.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx
from pydantic import BaseModel


class Button(BaseModel):
    text: str
    callback_data: str


class Update(BaseModel):
    update_id: int
    chat_id: str = ""
    user_id: str = ""
    text: str = ""
    callback_id: str = ""
    callback_data: str = ""


class TelegramAdapter(Protocol):
    def send_message(self, text: str, buttons: list[Button] | None = None) -> str: ...
    def get_updates(self, offset: int | None) -> list[Update]: ...
    def answer_callback(self, callback_id: str, text: str) -> None: ...


class MockTelegramAdapter:
    def __init__(self, auto_approve: bool = True) -> None:
        self.auto_approve = auto_approve
        self.sent: list[dict[str, Any]] = []
        self._counter = 0

    def send_message(self, text: str, buttons: list[Button] | None = None) -> str:
        self._counter += 1
        self.sent.append({"id": str(self._counter), "text": text,
                          "buttons": [b.model_dump() for b in buttons or []]})
        line = f"[telegram-mock] {text}" + (f"  botões={[b.text for b in buttons]}" if buttons else "")
        try:
            print(line)
        except UnicodeEncodeError:  # console sem UTF-8 (pipe no Windows)
            print(line.encode("ascii", "replace").decode())
        return str(self._counter)

    def get_updates(self, offset: int | None) -> list[Update]:
        return []

    def answer_callback(self, callback_id: str, text: str) -> None:
        return None


def parse_updates(raw: list[dict[str, Any]], chat_id: str, operators: set[str]) -> list[Update]:
    """Converte o JSON do Bot API em Updates, descartando chats e usuários não autorizados."""
    updates: list[Update] = []
    for item in raw:
        if "callback_query" in item:
            cq = item["callback_query"]
            update = Update(update_id=item["update_id"], chat_id=str(cq["message"]["chat"]["id"]),
                            user_id=str(cq.get("from", {}).get("id", "")), callback_id=cq["id"],
                            callback_data=cq.get("data", ""))
        elif "message" in item:
            msg = item["message"]
            update = Update(update_id=item["update_id"], chat_id=str(msg["chat"]["id"]),
                            user_id=str(msg.get("from", {}).get("id", "")), text=msg.get("text", ""))
        else:
            continue
        if update.chat_id != chat_id:
            continue
        if operators and update.user_id not in operators:
            continue
        updates.append(update)
    return updates


class BotApiTelegramAdapter:
    def __init__(self, token: str, chat_id: str, operators: set[str] | None = None) -> None:
        if not token or not chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID são obrigatórios")
        self._base = f"https://api.telegram.org/bot{token}"
        self._chat_id = chat_id
        self._operators = set(operators or ())
        self._http = httpx.Client(timeout=35)

    def send_message(self, text: str, buttons: list[Button] | None = None) -> str:
        payload: dict[str, Any] = {"chat_id": self._chat_id, "text": text}
        if buttons:
            rows = [[b.model_dump() for b in buttons[i:i + 2]] for i in range(0, len(buttons), 2)]
            payload["reply_markup"] = {"inline_keyboard": rows}
        response = self._http.post(f"{self._base}/sendMessage", json=payload)
        response.raise_for_status()
        return str(response.json()["result"]["message_id"])

    def get_updates(self, offset: int | None) -> list[Update]:
        params: dict[str, Any] = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        response = self._http.get(f"{self._base}/getUpdates", params=params)
        response.raise_for_status()
        return parse_updates(response.json().get("result", []), self._chat_id, self._operators)

    def answer_callback(self, callback_id: str, text: str) -> None:
        self._http.post(f"{self._base}/answerCallbackQuery",
                        json={"callback_query_id": callback_id, "text": text})
