"""Workflow log fields are allowlisted. Never serialize action arguments or HTTP headers."""
import json
import logging
import os
from contextvars import ContextVar
from contextlib import contextmanager

from servicedesk_agent.agent.masking.redaction import redact_text
from servicedesk_agent.infra.monitor.telemetry import current_traceparent

_fields = ContextVar("workflow_log_fields", default={})


@contextmanager
def action_fields(action):
    token = _fields.set({"component": "worker", "action_id": action["id"],
                         "approval_id": action["approval_id"], "tenant": action["tenant"]})
    try:
        yield
    finally:
        _fields.reset(token)


class WorkflowFormatter(logging.Formatter):
    def __init__(self, component, json_format):
        super().__init__()
        self.component, self.json_format = component, json_format

    def format(self, record):
        parent = current_traceparent()
        fields = {"level": record.levelname, "logger": record.name, "message": redact_text(super().format(record)),
                  "trace_id": parent.split("-")[1] if parent else None, "component": self.component, **_fields.get()}
        return json.dumps(fields, ensure_ascii=False) if self.json_format else " ".join(f"{k}={v}" for k, v in fields.items())


def configure_logging(component):
    handler = logging.StreamHandler()
    handler.setFormatter(WorkflowFormatter(component, os.getenv("SERVICEDESK_AGENT_LOG_FORMAT", "json") == "json"))
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), handlers=[handler], force=True)
