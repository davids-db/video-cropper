"""
cleanup.py

Retention/cleanup endpoint.

Exposes:
  - POST /cleanup

Intended caller:
- Cloud Scheduler (once per day)

Behavior:
- Deletes Firestore job documents older than RETENTION_DAYS (default 14)

Security:
- Requires header X-Cleanup-Token == CLEANUP_TOKEN
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from flask import Blueprint, request, jsonify, current_app
from google.cloud import firestore

from logging_utils import setup_logging

cleanup_bp = Blueprint("cleanup", __name__)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@cleanup_bp.post("/cleanup")
def cleanup() -> Any:
    setup_logging()
    logger = logging.getLogger(__name__)

    cleanup_token = current_app.config.get("CLEANUP_TOKEN")
    if not cleanup_token:
        logger.error("cleanup_missing_token_config")
        return jsonify({"error": "Missing CLEANUP_TOKEN configuration"}), 500

    if request.headers.get("X-Cleanup-Token") != cleanup_token:
        return jsonify({"error": "Unauthorized"}), 401

    retention_days = int(os.environ.get("RETENTION_DAYS", "14"))
    stalled_minutes = int(os.environ.get("STALLED_MINUTES", "0"))
    cutoff = now_utc() - timedelta(days=retention_days)

    db: firestore.Client = current_app.config["DB"]
    collection = current_app.config["FIRESTORE_COLLECTION"]
    col = db.collection(collection)

    deleted = 0
    stalled_marked = 0
    stalled_cutoff = None

    if stalled_minutes > 0:
        stalled_cutoff = now_utc() - timedelta(minutes=stalled_minutes)
        for status in ("queued", "processing"):
            while True:
                docs = list(
                    col.where("status", "==", status)
                    .where("updated_at_ts", "<", stalled_cutoff)
                    .limit(500)
                    .stream()
                )
                if not docs:
                    break

                batch = db.batch()
                now_ts = now_utc()
                for d in docs:
                    batch.update(
                        d.reference,
                        {
                            "status": "failed",
                            "error": f"stalled: no update in {stalled_minutes} minutes",
                            "updated_at_ts": now_ts,
                        },
                    )
                    stalled_marked += 1
                batch.commit()

    while True:
        docs = list(col.where("created_at_ts", "<", cutoff).limit(500).stream())
        if not docs:
            break

        batch = db.batch()
        for d in docs:
            batch.delete(d.reference)
            deleted += 1
        batch.commit()

    logger.info(
        "cleanup_done deleted=%s cutoff=%s stalled_marked=%s stalled_cutoff=%s",
        deleted,
        cutoff.isoformat(),
        stalled_marked,
        stalled_cutoff.isoformat() if stalled_cutoff else None,
    )
    return (
        jsonify(
            {
                "ok": True,
                "deleted": deleted,
                "cutoff": cutoff.isoformat(),
                "stalled_marked": stalled_marked,
                "stalled_cutoff": stalled_cutoff.isoformat() if stalled_cutoff else None,
            }
        ),
        200,
    )
