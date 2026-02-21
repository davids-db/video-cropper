"""
api.py

Public API layer for the Cloud Run service.

Exposes:
  - GET  /health
  - POST /submit        -> returns job_id
  - GET  /status/<id>   -> returns status/result

Also registers internal endpoints via Flask Blueprints:
  - worker_bp:  POST /process   (invoked by Cloud Tasks)
  - cleanup_bp: POST /cleanup   (invoked by Cloud Scheduler)

Architecture:
1) Client calls POST /submit with input video URI.
2) API creates a Firestore job document and enqueues a Cloud Task.
3) Worker (POST /process) runs person-detection-based cropping and writes output back to GCS.
4) Client polls GET /status/<job_id>.
"""

from __future__ import annotations

import os
import json
import uuid
import logging
from datetime import datetime, timezone
from typing import Any, Dict

from flask import Flask, request, jsonify
from google.cloud import firestore
from google.cloud import tasks_v2
from google.protobuf import duration_pb2

from worker import worker_bp
from cleanup import cleanup_bp
from video_cropper import CropperConfig
from logging_utils import setup_logging


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def create_app() -> Flask:
    setup_logging()
    logger = logging.getLogger(__name__)

    app = Flask(__name__)
    app.register_blueprint(worker_bp)
    app.register_blueprint(cleanup_bp)

    # ---- Required env (allow startup even if missing; endpoints validate) ----
    project_id = os.environ.get("PROJECT_ID")
    service_url = os.environ.get("SERVICE_URL")
    tasks_invoker_sa_email = os.environ.get("TASKS_INVOKER_SA_EMAIL")
    process_token = os.environ.get("PROCESS_TOKEN")
    cleanup_token = os.environ.get("CLEANUP_TOKEN")

    # ---- Optional env ----
    region = os.environ.get("REGION", "us-central1")
    queue_name = os.environ.get("TASKS_QUEUE", "video-cropper-queue")
    collection = os.environ.get("FIRESTORE_COLLECTION", "video_crop_jobs")

    # Cropper config
    cfg = CropperConfig(
        model_name=os.environ.get("MODEL_NAME", "yolov8n.pt"),
        conf=float(os.environ.get("CONF", "0.25")),
        iou=float(os.environ.get("IOU", "0.5")),
        padding_ratio=float(os.environ.get("PADDING_RATIO", "0.12")),
        min_crop_ratio=float(os.environ.get("MIN_CROP_RATIO", "0.35")),
        smooth_alpha=float(os.environ.get("SMOOTH_ALPHA", "0.85")),
        keep_aspect=os.environ.get("KEEP_ASPECT", "1") not in ("0", "false", "False"),
        draw_timestamp=os.environ.get("DRAW_TIMESTAMP", "1") not in ("0", "false", "False"),
        detect_batch_size=int(os.environ.get("DETECT_BATCH_SIZE", "8")),
    )

    # Clients
    db = firestore.Client()
    tasks_client = tasks_v2.CloudTasksClient()

    app.config.update(
        DB=db,
        TASKS_CLIENT=tasks_client,
        PROJECT_ID=project_id,
        REGION=region,
        TASKS_QUEUE=queue_name,
        SERVICE_URL=service_url,
        TASKS_INVOKER_SA_EMAIL=tasks_invoker_sa_email,
        PROCESS_TOKEN=process_token,
        CLEANUP_TOKEN=cleanup_token,
        FIRESTORE_COLLECTION=collection,
        CROPPER_CONFIG=cfg,
    )

    if not project_id:
        logger.warning("PROJECT_ID is not set; Firestore operations will fail")
    if not service_url:
        logger.warning("SERVICE_URL is not set; /submit will fail until configured")
    if not tasks_invoker_sa_email:
        logger.warning("TASKS_INVOKER_SA_EMAIL is not set; /submit will fail until configured")
    if not process_token:
        logger.warning("PROCESS_TOKEN is not set; /process will reject all requests")
    if not cleanup_token:
        logger.warning("CLEANUP_TOKEN is not set; /cleanup will reject all requests")

    @app.get("/health")
    def health() -> Any:
        return jsonify({"ok": True}), 200

    @app.get("/gpu")
    def gpu_info() -> Any:
        import torch
        cuda_available = torch.cuda.is_available()
        info: Dict[str, Any] = {"cuda_available": cuda_available}
        if cuda_available:
            info["device_count"] = torch.cuda.device_count()
            info["device_name"] = torch.cuda.get_device_name(0)
            info["memory_total_mb"] = round(torch.cuda.get_device_properties(0).total_memory / 1024 ** 2)
        return jsonify(info), 200

    @app.post("/submit")
    def submit() -> Any:
        data: Dict[str, Any] = request.get_json(silent=True) or {}
        uri = data.get("uri")
        if not uri:
            return jsonify({"error": "Missing required field: uri"}), 400
        if not (uri.startswith("gs://") or uri.startswith("http://") or uri.startswith("https://")):
            return jsonify({"error": "Invalid URI scheme; expected gs://, http://, or https://"}), 400

        missing = [
            name
            for name in ("PROJECT_ID", "SERVICE_URL", "TASKS_INVOKER_SA_EMAIL", "PROCESS_TOKEN")
            if not app.config.get(name)
        ]
        if missing:
            logger.error("submit_failed_missing_config missing=%s", ",".join(missing))
            return jsonify({"error": "Missing required configuration", "missing": missing}), 500

        job_id = str(uuid.uuid4())
        ts = now_utc()

        # Persist job
        doc_ref = db.collection(collection).document(job_id)
        doc_ref.set(
            {
                "job_id": job_id,
                "uri": uri,
                "status": "queued",
                "created_at_ts": ts,
                "updated_at_ts": ts,
            }
        )

        # Enqueue Cloud Task to call /process
        parent = tasks_client.queue_path(project_id, region, queue_name)
        task = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{service_url}/process",
                "headers": {
                    "Content-Type": "application/json",
                    "X-Process-Token": process_token,  # defense-in-depth
                },
                "oidc_token": {
                    "service_account_email": tasks_invoker_sa_email,
                    "audience": service_url,
                },
                "body": json.dumps({"job_id": job_id}).encode(),
            },
            # Max dispatch deadline for HTTP targets (30 min). Cloud Tasks will not
            # retry the task until this deadline has elapsed, preventing duplicate
            # processing of long-running jobs.
            "dispatch_deadline": duration_pb2.Duration(seconds=1800),
        }
        tasks_client.create_task(parent=parent, task=task)

        return jsonify({"job_id": job_id, "status": "queued"}), 202

    @app.get("/status/<job_id>")
    def status(job_id: str) -> Any:
        doc_ref = db.collection(collection).document(job_id)
        snap = doc_ref.get()
        if not snap.exists:
            return jsonify({"error": "Not found", "job_id": job_id}), 404

        data = snap.to_dict() or {}
        out: Dict[str, Any] = {
            "job_id": job_id,
            "status": data.get("status"),
            "created_at_ts": getattr(data.get("created_at_ts"), "isoformat", lambda: None)(),
            "updated_at_ts": getattr(data.get("updated_at_ts"), "isoformat", lambda: None)(),
        }
        if data.get("status") == "done":
            out["result"] = data.get("result") or {}
        if data.get("status") == "failed":
            out["error"] = data.get("error")
        return jsonify(out), 200

    return app


app = create_app()
