"""Telegram como canal do operador: notificações, aprovações com botões e comandos."""

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


class BotApiTelegramAdapter:
    def __init__(self, token: str, chat_id: str) -> None:
        if not token or not chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID são obrigatórios")
        self._base = f"https://api.telegram.org/bot{token}"
        self._chat_id = chat_id
        self._http = httpx.Client(timeout=35)

    def send_message(self, text: str, buttons: list[Button] | None = None) -> str:
        payload: dict[str, Any] = {"chat_id": self._chat_id, "text": text}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[b.model_dump() for b in buttons]]}
        response = self._http.post(f"{self._base}/sendMessage", json=payload)
        response.raise_for_status()
        return str(response.json()["result"]["message_id"])

    def get_updates(self, offset: int | None) -> list[Update]:
        params: dict[str, Any] = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        response = self._http.get(f"{self._base}/getUpdates", params=params)
        response.raise_for_status()
        updates: list[Update] = []
        for item in response.json().get("result", []):
            if "callback_query" in item:
                cq = item["callback_query"]
                updates.append(Update(update_id=item["update_id"],
                                      chat_id=str(cq["message"]["chat"]["id"]),
                                      callback_id=cq["id"], callback_data=cq.get("data", "")))
            elif "message" in item:
                msg = item["message"]
                updates.append(Update(update_id=item["update_id"], chat_id=str(msg["chat"]["id"]),
                                      text=msg.get("text", "")))
        # só aceita o chat autorizado
        return [u for u in updates if u.chat_id == self._chat_id]

    def answer_callback(self, callback_id: str, text: str) -> None:
        self._http.post(f"{self._base}/answerCallbackQuery",
                        json={"callback_query_id": callback_id, "text": text})
