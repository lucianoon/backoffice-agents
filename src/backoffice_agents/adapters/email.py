from __future__ import annotations

import base64
import email as email_lib
import imaplib
import json
import smtplib
from email.header import decode_header, make_header
from email.message import EmailMessage as StdEmailMessage
from email.utils import make_msgid, parseaddr
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from ..config import Settings

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024


class Attachment(BaseModel):
    filename: str
    content_type: str
    data_b64: str = ""       # vazio quando o anexo foi ignorado por tamanho
    size: int = 0

    @property
    def data(self) -> bytes:
        return base64.b64decode(self.data_b64) if self.data_b64 else b""


class EmailMessage(BaseModel):
    id: str
    from_addr: str
    from_name: str = ""
    to: str = ""
    subject: str = ""
    body: str = ""
    date: str = ""
    message_id: str = ""
    in_reply_to: str = ""
    references: list[str] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)


class SentEmail(BaseModel):
    in_reply_to: str
    to: str
    subject: str
    body: str
    kind: str = "reply"  # reply | forward
    note: str = ""
    message_id: str = ""


class EmailAdapter(Protocol):
    def fetch_unread(self) -> list[EmailMessage]: ...
    def send_reply(self, original: EmailMessage, body: str) -> str: ...   # devolve o Message-ID enviado
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

    def send_reply(self, original: EmailMessage, body: str) -> str:
        message_id = f"<mock-{len(self.sent) + 1}@exemplo.com>"
        self.sent.append(SentEmail(in_reply_to=original.id, to=original.from_addr,
                                   subject=f"Re: {original.subject}", body=body, message_id=message_id))
        return message_id

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
        # timeout no socket também: sem isso uma conexão presa segura o worker
        conn = imaplib.IMAP4_SSL(self._s.imap_host, self._s.imap_port, timeout=30)
        conn.login(self._s.imap_user, self._s.imap_password)
        conn.select(self._s.imap_folder)
        return conn

    def fetch_unread(self) -> list[EmailMessage]:
        conn = self._imap()
        try:
            _, data = conn.search(None, "UNSEEN")
            # Os mais recentes primeiro, já limitados: uma caixa cheia não vira inundação.
            uids = data[0].split()[-self._s.imap_fetch_limit:]
            messages: list[EmailMessage] = []
            for uid in uids:
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
                    message_id=msg.get("Message-ID", "").strip(),
                    in_reply_to=msg.get("In-Reply-To", "").strip(),
                    references=msg.get("References", "").split(),
                    attachments=_attachments(msg),
                ))
            return messages
        finally:
            conn.logout()

    def send_reply(self, original: EmailMessage, body: str) -> str:
        msg = StdEmailMessage()
        msg["From"] = self._from
        msg["To"] = original.from_addr
        msg["Subject"] = f"Re: {original.subject}"
        msg["Message-ID"] = make_msgid()
        if original.message_id:
            msg["In-Reply-To"] = original.message_id
            msg["References"] = " ".join([*original.references, original.message_id])
        msg.set_content(body)
        self._send(msg)
        return msg["Message-ID"]

    def forward(self, original: EmailMessage, to: str, note: str) -> None:
        msg = StdEmailMessage()
        msg["From"] = self._from
        msg["To"] = to
        msg["Subject"] = f"Fwd: {original.subject}"
        msg["Message-ID"] = make_msgid()
        msg.set_content(f"{note}\n\n---------- Mensagem encaminhada ----------\n"
                        f"De: {original.from_name} <{original.from_addr}>\n"
                        f"Assunto: {original.subject}\n\n{original.body}")
        for att in original.attachments:
            if att.data_b64:
                maintype, _, subtype = att.content_type.partition("/")
                msg.add_attachment(att.data, maintype=maintype or "application",
                                   subtype=subtype or "octet-stream", filename=att.filename)
        self._send(msg)

    def mark_processed(self, message_id: str) -> None:
        # timeout no socket também (mesma proteção do _imap)
        conn = imaplib.IMAP4_SSL(self._s.imap_host, self._s.imap_port, timeout=30)
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


def _attachments(msg) -> list[Attachment]:
    found: list[Attachment] = []
    if not msg.is_multipart():
        return found
    for part in msg.walk():
        disposition = (part.get("Content-Disposition") or "").lower()
        if "attachment" not in disposition and not part.get_filename():
            continue
        payload = part.get_payload(decode=True) or b""
        filename = _decode(part.get_filename() or "anexo")
        if len(payload) > MAX_ATTACHMENT_BYTES:
            found.append(Attachment(filename=filename, content_type=part.get_content_type(),
                                    size=len(payload)))
        else:
            found.append(Attachment(filename=filename, content_type=part.get_content_type(),
                                    data_b64=base64.b64encode(payload).decode(), size=len(payload)))
    return found
