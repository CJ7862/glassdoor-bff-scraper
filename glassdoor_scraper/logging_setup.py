"""Structured logging setup shared by the CLI and API service.

Two modes:
  * plain text  -- the original ``%(asctime)s [%(levelname)s] %(message)s`` stream,
    used by the CLI so ``--debug`` output looks exactly like before.
  * JSON        -- one JSON object per line with a request/job id on every line,
    used by the API service so logs are machine-parseable.

A :class:`contextvars.ContextVar` carries the current request/job id so any log
line emitted while handling a request is automatically tagged, without threading
the id through every function call.
"""

from __future__ import annotations

import contextvars
import datetime as _dt
import json
import logging
from typing import Any

# Carries the active request/job id for the current async task or thread.
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "glassdoor_request_id", default=None
)

_RESERVED_LOGRECORD_KEYS = set(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime", "taskName"}


class RequestIdFilter(logging.Filter):
    """Inject the current request/job id onto every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        return True


class JsonLogFormatter(logging.Formatter):
    """Render log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": _dt.datetime.fromtimestamp(
                record.created, tz=_dt.UTC
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id

        # Attach any structured extras passed via ``logger.info(..., extra={...})``.
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOGRECORD_KEYS or key == "request_id":
                continue
            if key.startswith("_"):
                continue
            payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    """Configure the root logger.

    Idempotent: repeated calls replace the handler rather than stacking duplicates,
    which matters for the API (reload) and the test suite.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    if json_logs:
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
    root.addHandler(handler)


def set_request_id(request_id: str | None) -> contextvars.Token[str | None]:
    """Set the active request/job id, returning a token for later reset."""
    return request_id_var.set(request_id)


def reset_request_id(token: contextvars.Token[str | None]) -> None:
    """Restore the previous request/job id using a token from :func:`set_request_id`."""
    request_id_var.reset(token)
