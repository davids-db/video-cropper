"""
logging_utils.py

Central logging setup for the service.

On Cloud Run (K_SERVICE env var is set), emits one JSON object per line so
Cloud Logging can parse severity, message, and extra fields automatically.

Locally, emits human-readable text instead.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback


class _CloudRunFormatter(logging.Formatter):
    """
    Formats log records as JSON understood by Cloud Logging.

    Cloud Logging key names:
      severity  -> maps to the log level filter in Cloud Console
      message   -> main text shown in the log viewer
      logger    -> name of the Python logger
      exc       -> formatted traceback (only when an exception is attached)
    """

    LEVEL_MAP = {
        logging.DEBUG: "DEBUG",
        logging.INFO: "INFO",
        logging.WARNING: "WARNING",
        logging.ERROR: "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "severity": self.LEVEL_MAP.get(record.levelno, "DEFAULT"),
            "message": record.getMessage(),
            "logger": record.name,
        }
        if record.exc_info:
            entry["exc"] = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        return json.dumps(entry, ensure_ascii=False)


def setup_logging() -> None:
    """Configure root logging once per process.

    Uses JSON output when running on Cloud Run (K_SERVICE is set by the
    platform), plain text otherwise so local dev stays readable.
    """
    root = logging.getLogger()
    if root.handlers:
        return

    level = os.environ.get("LOG_LEVEL", "INFO").upper()

    if os.environ.get("K_SERVICE"):
        # Running on Cloud Run — emit JSON for Cloud Logging.
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_CloudRunFormatter())
    else:
        # Local / CI — human-readable text.
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
            stream=sys.stdout,
        )
        handler = None  # basicConfig already added a handler

    if handler:
        root.setLevel(level)
        root.addHandler(handler)

    # Reduce noisy third-party libraries unless overridden.
    logging.getLogger("google").setLevel(
        os.environ.get("GOOGLE_LOG_LEVEL", "WARNING").upper()
    )
    logging.getLogger("urllib3").setLevel(
        os.environ.get("URLLIB3_LOG_LEVEL", "WARNING").upper()
    )
    logging.getLogger("ultralytics").setLevel(
        os.environ.get("ULTRALYTICS_LOG_LEVEL", "WARNING").upper()
    )
