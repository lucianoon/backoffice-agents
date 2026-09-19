from __future__ import annotations

import email as email_lib
import imaplib
import json
import smtplib
from email.header import decode_header, make_header
from email.message import EmailMessage as StdEmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from ..config import Settings


class EmailMessage(BaseModel):
    id: str
    from_addr: str
    from_name: str = ""
    to: str = ""
    subject: str = ""
    body: str = ""
    date: str = ""
    message_id: str = ""


class SentEmail(BaseModel):
    in_reply_to: str
    to: str
    subject: str
    body: str
    kind: str = "reply"  # reply | forward
    note: str = ""


class EmailAdapter(Protocol):
    def fetch_unread(self) -> list[EmailMessage]: ...
    def send_reply(self, original: EmailMessage, body: str) -> None: ...
    def forward(self, original: EmailMessage, to: str, note: str) -> None: ...
    def mark_processed(self, message_id: str) -> None: ...


class MockEmailAdapter:
    """Lê e-mails de um JSON de exemplos e guarda os envios em memória."""

    def __init__(self, samples_path: str) -> None:
        self._path = Path(samples_path)
        self._processed: set[str] = set()
        self.sent: list[SentEmail] = []

    def fetch_unread(self) -> list[EmailMessage]:
        if not self._path.exists():
            return []
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        return [EmailMessage(**item) for item in raw if item["id"] not in self._processed]

    def send_reply(self, original: EmailMessage, body: str) -> None:
        self.sent.append(SentEmail(in_reply_to=original.id, to=original.from_addr,
                                   subject=f"Re: {original.subject}", body=body))

    def forward(self, original: EmailMessage, to: str, note: str) -> None:
        self.sent.append(SentEmail(in_reply_to=original.id, to=to, subject=f"Fwd: {original.subject}",
                                   body=original.body, kind="forward", note=note))

    def mark_processed(self, message_id: str) -> None:
        self._processed.add(message_id)


class ImapSmtpEmailAdapter:
    """IMAP para ler (não marca como lido até mark_processed) e SMTP para enviar."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        if not (settings.imap_user and settings.imap_password and settings.smtp_user
                and settings.smtp_password):
            raise ValueError("IMAP_USER/IMAP_PASSWORD/SMTP_USER/SMTP_PASSWORD são obrigatórios")
        self._from = settings.email_from or settings.smtp_user

    def _imap(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(self._s.imap_host, self._s.imap_port)
        conn.login(self._s.imap_user, self._s.imap_password)
        conn.select(self._s.imap_folder)
        return conn

    def fetch_unread(self) -> list[EmailMessage]:
        conn = self._imap()
        try:
            _, data = conn.search(None, "UNSEEN")
            messages: list[EmailMessage] = []
            for uid in data[0].split():
                # BODY.PEEK não marca como lido
                _, parts = conn.fetch(uid, "(BODY.PEEK[])")
                msg = email_lib.message_from_bytes(parts[0][1])
                name, addr = parseaddr(msg.get("From", ""))
                messages.append(EmailMessage(
                    id=uid.decode(),
                    from_addr=addr,
                    from_name=_decode(name),
                    to=msg.get("To", ""),
                    subject=_decode(msg.get("Subject", "")),
                    body=_text_body(msg),
                    date=msg.get("Date", ""),
                    message_id=msg.get("Message-ID", ""),
                ))
            return messages
        finally:
            conn.logout()

    def send_reply(self, original: EmailMessage, body: str) -> None:
        msg = StdEmailMessage()
        msg["From"] = self._from
        msg["To"] = original.from_addr
        msg["Subject"] = f"Re: {original.subject}"
        if original.message_id:
            msg["In-Reply-To"] = original.message_id
            msg["References"] = original.message_id
        msg.set_content(body)
        self._send(msg)

    def forward(self, original: EmailMessage, to: str, note: str) -> None:
        msg = StdEmailMessage()
        msg["From"] = self._from
        msg["To"] = to
        msg["Subject"] = f"Fwd: {original.subject}"
        msg.set_content(f"{note}\n\n---------- Mensagem encaminhada ----------\n"
                        f"De: {original.from_name} <{original.from_addr}>\n"
                        f"Assunto: {original.subject}\n\n{original.body}")
        self._send(msg)

    def mark_processed(self, message_id: str) -> None:
        conn = self._imap()
        try:
            conn.store(message_id.encode(), "+FLAGS", "\\Seen")
        finally:
            conn.logout()

    def _send(self, msg: StdEmailMessage) -> None:
        with smtplib.SMTP(self._s.smtp_host, self._s.smtp_port) as smtp:
            smtp.starttls()
            smtp.login(self._s.smtp_user, self._s.smtp_password)
            smtp.send_message(msg)


def _decode(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _text_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        return ""
    payload = msg.get_payload(decode=True)
    return payload.decode(msg.get_content_charset() or "utf-8", "replace") if payload else ""
