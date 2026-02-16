"""
video_cropper.py

Core pipeline:
  - download input video (gs:// or http(s)://)
  - run person detection per frame
  - compute a stable crop window that removes irrelevant frame regions
  - optionally draw a per-frame timestamp on the top-right
  - encode output video
  - upload output back to storage (same bucket/path for gs:// inputs, or OUTPUT_BUCKET for http inputs)

Notes:
- This implementation uses Ultralytics YOLO (torch-based). For CPU-only Cloud Run,
  keep the model small (default: yolov8n.pt) and set max-instances=1, concurrency=1.
"""

from __future__ import annotations

import os
import math
import shutil
import logging
import tempfile
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any

import cv2  # type: ignore
import numpy as np  # type: ignore
import requests
from google.cloud import storage

from ultralytics import YOLO  # type: ignore


class ProcessingError(RuntimeError):
    pass


class DownloadError(ProcessingError):
    pass


@dataclass(frozen=True)
class CropperConfig:
    # Detection
    model_name: str = "yolov8n.pt"  # downloaded automatically by ultralytics
    conf: float = 0.25
    iou: float = 0.5

    # Crop behavior
    padding_ratio: float = 0.12   # padding around union-of-people box relative to box size
    min_crop_ratio: float = 0.35  # minimum crop area vs full frame (avoid tiny crops)
    smooth_alpha: float = 0.85    # EMA smoothing; higher = steadier
    keep_aspect: bool = True      # keep original aspect ratio
    max_upscale: float = 1.0      # don't upscale crop beyond original size

    # Timestamp overlay
    draw_timestamp: bool = True
    timestamp_font_scale: float = 0.8
    timestamp_thickness: int = 2
    timestamp_margin_px: int = 12

    # IO
    tmp_dir: str = "/tmp"


def _parse_gs_uri(uri: str) -> Tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError("Not a gs:// URI")
    parts = uri[5:].split("/", 1)
    bucket = parts[0]
    blob = parts[1] if len(parts) > 1 else ""
    return bucket, blob


def _splitext_gs_path(blob_name: str) -> Tuple[str, str]:
    # Split extension while preserving nested paths.
    base, ext = os.path.splitext(blob_name)
    return base, ext


class VideoIO:
    """Handles download/upload for gs:// and http(s):// URIs."""

    def __init__(self, storage_client: storage.Client, logger: logging.Logger):
        self._storage = storage_client
        self._logger = logger

    def download(self, uri: str, dst_path: str) -> Dict[str, Any]:
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)

        if uri.startswith("gs://"):
            bucket_name, blob_name = _parse_gs_uri(uri)
            self._logger.info("download_gcs uri=%s", uri)
            bucket = self._storage.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            blob.download_to_filename(dst_path)
            return {"scheme": "gs", "bucket": bucket_name, "blob": blob_name}

        if uri.startswith(("http://", "https://")):
            self._logger.info("download_http uri=%s", uri)
            with requests.get(uri, stream=True, timeout=600) as r:
                r.raise_for_status()
                with open(dst_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
            return {"scheme": "http", "url": uri}

        raise DownloadError(f"Unsupported URI scheme: {uri}")

    def output_uri_for_input(self, input_uri: str, output_bucket_for_http: Optional[str]) -> str:
        if input_uri.startswith("gs://"):
            b, blob = _parse_gs_uri(input_uri)
            base, ext = _splitext_gs_path(blob)
            out_blob = f"{base}_cropped{ext or '.mp4'}"
            return f"gs://{b}/{out_blob}"

        # For http(s) inputs, we cannot write back to the origin server.
        # Use OUTPUT_BUCKET and put under a deterministic name.
        if not output_bucket_for_http:
            raise ProcessingError("HTTP(S) input requires OUTPUT_BUCKET to be set for output writes.")
        # best-effort basename
        name = os.path.basename(input_uri.split("?", 1)[0]) or "video.mp4"
        base, ext = os.path.splitext(name)
        out_name = f"{base}_cropped{ext or '.mp4'}"
        return f"gs://{output_bucket_for_http}/{out_name}"

    def upload(self, local_path: str, output_uri: str) -> None:
        if not output_uri.startswith("gs://"):
            raise ProcessingError("Output URI must be gs://")
        b, blob = _parse_gs_uri(output_uri)
        self._logger.info("upload_gcs output_uri=%s", output_uri)
        bucket = self._storage.bucket(b)
        bucket.blob(blob).upload_from_filename(local_path)


class PersonDetector:
    """Person detector wrapper."""

    def __init__(self, cfg: CropperConfig, logger: logging.Logger):
        self._cfg = cfg
        self._logger = logger
        self._model: Optional[YOLO] = None

    def _load(self) -> YOLO:
        if self._model is None:
            self._logger.info("loading_model name=%s", self._cfg.model_name)
            self._model = YOLO(self._cfg.model_name)
        return self._model

    def detect_union_xyxy(self, frame_bgr: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
        """
        Returns union bounding box for detected persons in XYXY pixel coords.
        """
        model = self._load()

        # Ultralytics accepts BGR numpy arrays; internally converts.
        res = model.predict(
            frame_bgr,
            classes=[0],  # person
            conf=self._cfg.conf,
            iou=self._cfg.iou,
            verbose=False,
        )

        if not res or res[0].boxes is None or len(res[0].boxes) == 0:
            return None

        boxes = res[0].boxes.xyxy.cpu().numpy()
        x1 = float(np.min(boxes[:, 0]))
        y1 = float(np.min(boxes[:, 1]))
        x2 = float(np.max(boxes[:, 2]))
        y2 = float(np.max(boxes[:, 3]))
        return int(x1), int(y1), int(x2), int(y2)


class CropWindowSmoother:
    """EMA smoother for crop windows to reduce jitter."""

    def __init__(self, alpha: float):
        self._alpha = float(alpha)
        self._prev: Optional[Tuple[float, float, float, float]] = None

    def update(self, box: Tuple[float, float, float, float]) -> Tuple[float, float, float, float]:
        if self._prev is None:
            self._prev = box
            return box
        a = self._alpha
        px1, py1, px2, py2 = self._prev
        x1, y1, x2, y2 = box
        smoothed = (a * px1 + (1 - a) * x1, a * py1 + (1 - a) * y1, a * px2 + (1 - a) * x2, a * py2 + (1 - a) * y2)
        self._prev = smoothed
        return smoothed

    def get(self) -> Optional[Tuple[float, float, float, float]]:
        return self._prev


class VideoCropper:
    """End-to-end crop pipeline."""

    def __init__(self, cfg: CropperConfig, logger: logging.Logger):
        self.cfg = cfg
        self.logger = logger
        self.storage_client = storage.Client()
        self.io = VideoIO(self.storage_client, logger)
        self.detector = PersonDetector(cfg, logger)
        self.smoother = CropWindowSmoother(cfg.smooth_alpha)

    def run(self, input_uri: str) -> Dict[str, Any]:
        job_tmp = tempfile.mkdtemp(prefix="video-crop-", dir=self.cfg.tmp_dir)
        in_path = os.path.join(job_tmp, "input.mp4")
        out_path = os.path.join(job_tmp, "output.mp4")

        output_bucket_for_http = os.environ.get("OUTPUT_BUCKET")
        output_uri = self.io.output_uri_for_input(input_uri, output_bucket_for_http)

        meta = {"input_uri": input_uri, "output_uri": output_uri}

        try:
            self.io.download(input_uri, in_path)
            self._process_video(in_path, out_path)
            self.io.upload(out_path, output_uri)
            return meta
        finally:
            shutil.rmtree(job_tmp, ignore_errors=True)

    def _process_video(self, in_path: str, out_path: str) -> None:
        cap = cv2.VideoCapture(in_path)
        if not cap.isOpened():
            raise ProcessingError("Failed to open input video")

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

        self.logger.info("video_info fps=%.3f w=%d h=%d frames=%d", fps, width, height, frame_count)

        # Use mp4v; broadly supported on Cloud Run.
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(out_path, fourcc, fps, (width, height))
        if not writer.isOpened():
            cap.release()
            raise ProcessingError("Failed to open output VideoWriter")

        try:
            idx = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break

                crop = self._compute_crop(frame)
                cropped = self._crop_and_letterbox(frame, crop, (width, height))

                if self.cfg.draw_timestamp:
                    self._draw_timestamp(cropped, idx, fps)

                writer.write(cropped)

                idx += 1
                if idx % 60 == 0:
                    self.logger.info("processed_frames n=%d", idx)
        finally:
            cap.release()
            writer.release()

    def _compute_crop(self, frame: np.ndarray) -> Tuple[int, int, int, int]:
        h, w = frame.shape[:2]
        det = self.detector.detect_union_xyxy(frame)

        if det is None:
            prev = self.smoother.get()
            if prev is not None:
                x1, y1, x2, y2 = prev
                return int(x1), int(y1), int(x2), int(y2)
            return 0, 0, w, h

        x1, y1, x2, y2 = det

        # padding
        bw = max(1, x2 - x1)
        bh = max(1, y2 - y1)
        pad_x = int(self.cfg.padding_ratio * bw)
        pad_y = int(self.cfg.padding_ratio * bh)

        x1 = max(0, x1 - pad_x)
        y1 = max(0, y1 - pad_y)
        x2 = min(w, x2 + pad_x)
        y2 = min(h, y2 + pad_y)

        # enforce min crop ratio (area)
        min_area = self.cfg.min_crop_ratio * (w * h)
        cur_area = max(1, (x2 - x1) * (y2 - y1))
        if cur_area < min_area:
            # expand around center
            cx = (x1 + x2) / 2.0
            cy = (y1 + y2) / 2.0
            target_area = min_area
            target_side = math.sqrt(target_area)  # approx square; will be aspect corrected below
            tw = target_side
            th = target_side
            x1 = int(max(0, cx - tw / 2))
            x2 = int(min(w, cx + tw / 2))
            y1 = int(max(0, cy - th / 2))
            y2 = int(min(h, cy + th / 2))

        # aspect keep: expand crop window to match original aspect
        if self.cfg.keep_aspect:
            target_aspect = w / h
            cw = max(1, x2 - x1)
            ch = max(1, y2 - y1)
            cur_aspect = cw / ch

            cx = (x1 + x2) / 2.0
            cy = (y1 + y2) / 2.0

            if cur_aspect > target_aspect:
                # too wide -> increase height
                new_h = cw / target_aspect
                y1 = int(max(0, cy - new_h / 2))
                y2 = int(min(h, cy + new_h / 2))
            else:
                # too tall -> increase width
                new_w = ch * target_aspect
                x1 = int(max(0, cx - new_w / 2))
                x2 = int(min(w, cx + new_w / 2))

        # clamp and smooth
        x1, y1, x2, y2 = self._clamp_box((x1, y1, x2, y2), w, h)
        sx1, sy1, sx2, sy2 = self.smoother.update((float(x1), float(y1), float(x2), float(y2)))
        return self._clamp_box((int(sx1), int(sy1), int(sx2), int(sy2)), w, h)

    @staticmethod
    def _clamp_box(box: Tuple[int, int, int, int], w: int, h: int) -> Tuple[int, int, int, int]:
        x1, y1, x2, y2 = box
        x1 = max(0, min(w - 1, x1))
        y1 = max(0, min(h - 1, y1))
        x2 = max(x1 + 1, min(w, x2))
        y2 = max(y1 + 1, min(h, y2))
        return x1, y1, x2, y2

    def _crop_and_letterbox(self, frame: np.ndarray, crop: Tuple[int, int, int, int], out_size: Tuple[int, int]) -> np.ndarray:
        """
        Crop and then resize back to original size (letterboxing if needed to preserve aspect).
        Output is always the original WxH so downstream users don't have to handle varying dims.
        """
        out_w, out_h = out_size
        x1, y1, x2, y2 = crop
        cropped = frame[y1:y2, x1:x2]

        if cropped.size == 0:
            return frame

        # Prevent upscaling beyond original by limiting scale (optional).
        ch, cw = cropped.shape[:2]
        scale = min(out_w / cw, out_h / ch)
        if self.cfg.max_upscale <= 1.0:
            scale = min(scale, 1.0)

        new_w = max(1, int(cw * scale))
        new_h = max(1, int(ch * scale))
        resized = cv2.resize(cropped, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

        canvas = np.zeros((out_h, out_w, 3), dtype=np.uint8)
        x_off = (out_w - new_w) // 2
        y_off = (out_h - new_h) // 2
        canvas[y_off:y_off + new_h, x_off:x_off + new_w] = resized
        return canvas

    def _draw_timestamp(self, frame: np.ndarray, idx: int, fps: float) -> None:
        t = idx / fps
        hh = int(t // 3600)
        mm = int((t % 3600) // 60)
        ss = int(t % 60)
        ms = int((t - int(t)) * 1000)
        label = f"{hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}"

        h, w = frame.shape[:2]
        margin = int(self.cfg.timestamp_margin_px)

        font = cv2.FONT_HERSHEY_SIMPLEX
        (tw, th), baseline = cv2.getTextSize(label, font, self.cfg.timestamp_font_scale, self.cfg.timestamp_thickness)
        x = max(margin, w - margin - tw)
        y = max(margin + th, margin + th)

        # Draw a filled background rectangle for readability.
        cv2.rectangle(frame, (x - 6, y - th - 6), (x + tw + 6, y + baseline + 6), (0, 0, 0), thickness=-1)
        cv2.putText(frame, label, (x, y), font, self.cfg.timestamp_font_scale, (255, 255, 255), self.cfg.timestamp_thickness, cv2.LINE_AA)
