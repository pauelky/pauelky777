from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict


class JsonLogFormatter(logging.Formatter):
    """Structured log formatter for centralized JSONL logging."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        extra_fields = getattr(record, "extra_fields", None)
        if isinstance(extra_fields, dict):
            payload.update({k: _json_safe(v) for k, v in extra_fields.items()})

        return json.dumps(payload, ensure_ascii=False)


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def configure_centralized_logging(logs_dir: str, *, logger_name: str = "bot_main") -> None:
    """Attach a rotating JSONL handler to the shared application logger."""
    os.makedirs(logs_dir, exist_ok=True)
    log_path = os.path.abspath(os.path.join(logs_dir, "api_runtime.jsonl"))
    logger = logging.getLogger(logger_name)

    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler) and os.path.abspath(getattr(handler, "baseFilename", "")) == log_path:
            return

    handler = RotatingFileHandler(
        log_path,
        maxBytes=8 * 1024 * 1024,
        backupCount=4,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(JsonLogFormatter())
    logger.addHandler(handler)


def audit_log(logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Log structured audit events without breaking classic logger usage."""
    payload = {"event": event}
    payload.update({k: _json_safe(v) for k, v in fields.items()})
    logger.log(level, event, extra={"extra_fields": payload})
