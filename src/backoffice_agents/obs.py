"""Logs estruturados (JSON ou texto) com campos por evento."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

LOGGER_NAME = "backoffice"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
        }
        payload.update(getattr(record, "fields", {}) or {})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", {}) or {}
        extra = " ".join(f"{k}={v}" for k, v in fields.items())
        stamp = datetime.fromtimestamp(record.created).strftime('%H:%M:%S')
        base = f"{stamp} {record.levelname:<7} {record.getMessage()}"
        return f"{base} {extra}".rstrip()


def setup_logging(fmt: str = "text", level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level.upper())
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
        logger.addHandler(handler)
    else:
        logger.handlers[0].setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    return logger


def log_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    logging.getLogger(LOGGER_NAME).log(level, event, extra={"fields": fields})
