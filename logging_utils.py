"""
logging_utils.py

Central logging setup for the service.
"""

from __future__ import annotations

import logging
import os
import sys


def setup_logging() -> None:
    """Configure root logging once."""
    root = logging.getLogger()
    if root.handlers:
        return

    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    # Reduce noisy libraries unless explicitly set.
    logging.getLogger("google").setLevel(os.environ.get("GOOGLE_LOG_LEVEL", "WARNING").upper())
    logging.getLogger("urllib3").setLevel(os.environ.get("URLLIB3_LOG_LEVEL", "WARNING").upper())
    logging.getLogger("ultralytics").setLevel(os.environ.get("ULTRALYTICS_LOG_LEVEL", "WARNING").upper())
