"""
worker.py

Cloud Tasks worker endpoint.

Exposes:
  - POST /process

Responsibilities:
- Authenticate request (defense-in-depth header + IAM OIDC)
- Load job from Firestore
- Mark as processing
- Run video cropping pipeline
- Write result or error back to Firestore
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from flask import Blueprint, request, jsonify, current_app
from google.cloud import firestore

from video_cropper import VideoCropper, CropperConfig, ProcessingError
from logging_utils import setup_logging

worker_bp = Blueprint("worker", __name__)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def get_cropper() -> VideoCropper:
    """
    Return a singleton VideoCropper instance.

    Model loading is expensive; do it once per container process.
    """
    cropper = current_app.extensions.get("cropper")
    if cropper is None:
        cfg: CropperConfig = current_app.config["CROPPER_CONFIG"]
        cropper = VideoCropper(cfg, logging.getLogger("video_cropper"))
        current_app.extensions["cropper"] = cropper
    return cropper


@worker_bp.post("/process")
def process() -> Any:
    setup_logging()
    logger = logging.getLogger(__name__)

    process_token = current_app.config.get("PROCESS_TOKEN")
    if not process_token:
        logger.error("process_missing_token_config")
        return jsonify({"ok": False, "error": "Missing PROCESS_TOKEN configuration"}), 500

    if request.headers.get("X-Process-Token") != process_token:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    data: Dict[str, Any] = request.get_json(silent=True) or {}
    job_id = data.get("job_id")
    if not job_id:
        return jsonify({"ok": False, "error": "Missing job_id"}), 400

    db: firestore.Client = current_app.config["DB"]
    collection = current_app.config["FIRESTORE_COLLECTION"]
    doc_ref = db.collection(collection).document(job_id)

    snap = doc_ref.get()
    if not snap.exists:
        return jsonify({"ok": False, "error": "Job not found"}), 404

    job = snap.to_dict() or {}
    uri = job.get("uri")
    if not uri:
        doc_ref.update({"status": "failed", "error": "Job missing uri", "updated_at_ts": now_utc()})
        return jsonify({"ok": False, "error": "Job missing uri"}), 200

    # Mark processing
    doc_ref.update({"status": "processing", "updated_at_ts": now_utc()})

    try:
        cropper = get_cropper()
        meta = cropper.run(uri)
        doc_ref.update(
            {
                "status": "done",
                "result": {"output_uri": meta["output_uri"]},
                "updated_at_ts": now_utc(),
            }
        )
        return jsonify({"ok": True}), 200
    except ProcessingError as e:
        logger.exception("job_failed_processing_error job_id=%s", job_id)
        doc_ref.update({"status": "failed", "error": str(e), "updated_at_ts": now_utc()})
        return jsonify({"ok": False, "error": str(e)}), 200
    except Exception as e:
        logger.exception("job_failed_unexpected job_id=%s", job_id)
        doc_ref.update({"status": "failed", "error": f"unexpected: {e}", "updated_at_ts": now_utc()})
        return jsonify({"ok": False, "error": "unexpected"}), 200
