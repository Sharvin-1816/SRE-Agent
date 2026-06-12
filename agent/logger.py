"""
agent/logger.py

Structured JSON logger for all SRE Agent components.
Writes to logs/ directory which Promtail ships to Loki.
Loki stores them and Grafana displays them alongside metrics.

Usage:
    from agent.logger import get_logger
    log = get_logger("collector")
    log.info("Anomaly detected", service="payment_service", score=72.3)
    log.error("DB write failed", error=str(e))

Log files:
    logs/collector.log  → collector + anomaly detector
    logs/services.log   → mock services
    logs/webhook.log    → webhook receiver
    logs/agent.log      → agent loop + memory
"""

import os
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
LOG_DIR.mkdir(exist_ok=True)

_file_handles: dict[str, object] = {}
_lock = threading.Lock()

VALID_COMPONENTS = {"collector", "services", "webhook", "agent"}


def _get_file(component: str):
    """Get or create a file handle for this component."""
    if component not in _file_handles:
        path = LOG_DIR / f"{component}.log"
        _file_handles[component] = open(path, "a", buffering=1, encoding="utf-8")
    return _file_handles[component]


def _write(component: str, level: str, message: str, **kwargs):
    """Write a structured JSON log entry."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "component": component,
        "level":     level,
        "message":   message,
        **kwargs,
    }
    line = json.dumps(entry, default=str)
    with _lock:
        try:
            f = _get_file(component)
            f.write(line + "\n")
            f.flush()
        except Exception:
            pass


class Logger:
    """Structured logger for a single component."""

    def __init__(self, component: str):
        self.component = component

    def info(self, message: str, **kwargs):
        _write(self.component, "info", message, **kwargs)

    def warning(self, message: str, **kwargs):
        _write(self.component, "warning", message, **kwargs)

    def error(self, message: str, **kwargs):
        _write(self.component, "error", message, **kwargs)

    def debug(self, message: str, **kwargs):
        _write(self.component, "debug", message, **kwargs)


def get_logger(component: str) -> Logger:
    """Get a logger for a component. Component must be one of the valid ones."""
    if component not in VALID_COMPONENTS:
        component = "agent"
    return Logger(component)
