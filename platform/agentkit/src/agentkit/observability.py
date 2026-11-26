"""Structured JSON logging with trace correlation and PII scrubbing.

Every log line carries `trace_id` (propagated to Langfuse / OTel / CloudWatch) and passes
through the redactor, so a careless `log.info(f"... {claim_text}")` cannot leak an SSN.
"""

from __future__ import annotations

import contextvars
import json
import logging
import uuid
from typing import Any

from .guardrails import redact

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")


def new_trace_id() -> str:
    tid = uuid.uuid4().hex
    trace_id_var.set(tid)
    return tid


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": trace_id_var.get(),
            "msg": redact(record.getMessage()),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update({k: redact(v) if isinstance(v, str) else v for k, v in extra.items()})
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def get_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(JsonFormatter())
        log.addHandler(h)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


def langfuse_callback() -> Any | None:
    """LangChain/LangGraph callback handler when Langfuse is configured, else None."""
    try:
        from langfuse.langchain import CallbackHandler  # type: ignore[import-not-found]
    except ImportError:
        return None
    import os

    return CallbackHandler() if os.getenv("LANGFUSE_PUBLIC_KEY") else None
