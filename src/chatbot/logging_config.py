"""
logging_config.py - single source of truth for how the app logs.

Render (and most PaaS log dashboards) only capture stdout/stderr - they never
see files the process writes to its own disk. So every log line that should
be visible in the Render "Logs" tab has to go through Python's `logging`
module to a stdout stream handler, formatted as one JSON object per line.

That JSON-lines shape matters even for a single-instance app: Render's log
search/filter works on text, and structured fields (request_id, stage,
status, duration_s, ...) are what let you `grep`/search for one request's
whole story or filter by status instead of eyeballing free-text messages.

Call configure_logging() once, at process start, before any other module
logs anything.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

# Attributes every LogRecord has by default. Anything else on the record
# came from a caller's `extra={...}` and should be surfaced in the JSON body.
_RESERVED_RECORD_ATTRS = set(
    logging.LogRecord(
        name="", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None
    ).__dict__.keys()
) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Renders each LogRecord as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_ATTRS:
                payload[key] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        # default=str: extra fields are usually plain JSON types, but this
        # keeps a stray object (e.g. an exception instance) from crashing
        # the logger instead of just printing.
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> None:
    """
    Configure the root logger once, at process start.

    LOG_LEVEL (env var, default INFO) controls verbosity in every
    environment without a code change - e.g. set LOG_LEVEL=DEBUG on Render
    temporarily to see per-chunk retrieval scores while investigating an
    issue, then drop it back to INFO.
    """
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()  # idempotent: safe to call more than once

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
