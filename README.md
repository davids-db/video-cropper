# video-cropper-service

Person-detection-based video cropper as a Cloud Run service.

**Async job model**: `POST /submit` enqueues a Cloud Task that calls `POST /process`, while job state/results live in Firestore.
This mirrors the ivrit-transcriber pattern (Cloud Tasks + Firestore + cleanup). fileciteturn4file4L15-L19

## What this repo provides

- **HTTP API** for submitting crop jobs and polling status.
- **Async worker** endpoint invoked by Cloud Tasks.
- **Cleanup endpoint** to delete old jobs via Cloud Scheduler.

## HTTP API

Base URL is your Cloud Run service URL.

### `GET /health`
Returns `{"ok": true}`.

### `POST /submit`
Request:
```json
{"uri": "gs://bucket/path/video.mp4"}
```
Response (202):
```json
{"job_id":"<uuid>","status":"queued"}
```

### `GET /status/<job_id>`
Response:
- queued/processing:
  ```json
  {"job_id":"...","status":"processing", "created_at_ts":"...", "updated_at_ts":"..."}
  ```
- done:
  ```json
  {"job_id":"...","status":"done","result":{"output_uri":"gs://.../video_cropped.mp4"}}
  ```
- failed:
  ```json
  {"job_id":"...","status":"failed","error":"..."}
  ```

### `POST /process` (internal)
Cloud Tasks worker endpoint. Protected by IAM + `X-Process-Token` header. fileciteturn4file8L48-L50

### `POST /cleanup` (internal)
Cloud Scheduler endpoint. Protected by `X-Cleanup-Token` header. fileciteturn4file8L51-L52

## Input / Output rules

- Input supports:
  - `gs://bucket/path.mp4`
  - `http(s)://...` (download only)
- Output:
  - For `gs://` inputs: writes to the same bucket/path with `_cropped` suffix.
  - For `http(s)://` inputs: set `OUTPUT_BUCKET` and output will be written to `gs://$OUTPUT_BUCKET/<basename>_cropped.mp4`.

## Environment variables

Required:
- `PROJECT_ID`
- `SERVICE_URL`
- `TASKS_INVOKER_SA_EMAIL`
- `PROCESS_TOKEN`
- `CLEANUP_TOKEN`

Optional:
- `REGION` (default `us-central1`)
- `TASKS_QUEUE` (default `video-cropper-queue`)
- `FIRESTORE_COLLECTION` (default `video_crop_jobs`)
- `RETENTION_DAYS` (default `14`)
- `STALLED_MINUTES` (default `0`, disabled)

Cropper tuning:
- `MODEL_NAME` (default `yolov8n.pt`)
- `CONF` (default `0.25`)
- `IOU` (default `0.5`)
- `PADDING_RATIO` (default `0.12`)
- `MIN_CROP_RATIO` (default `0.35`)
- `SMOOTH_ALPHA` (default `0.85`)
- `KEEP_ASPECT` (default `1`)
- `DRAW_TIMESTAMP` (default `1`)
- `OUTPUT_BUCKET` (required for http(s) inputs)

## Deployment on GCP

### 1) Set up permissions
```bash
chmod +x scripts/*.sh
./scripts/permissions.sh
```

### 2) Deploy Cloud Run
```bash
ENV_FILE=.env.cloudrun ./scripts/deploy.sh
```

### 3) Schedule cleanup
```bash
export SERVICE_URL="https://..."
export CLEANUP_TOKEN="..."
./scripts/scheduler.sh
```

### 4) Initialize Firestore (first time only)
```bash
gcloud firestore databases create --location=us-central1
```

## Local dev quickstart
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export PROJECT_ID="your-project"
export SERVICE_URL="http://localhost:8080"
export TASKS_INVOKER_SA_EMAIL="your-sa@project.iam.gserviceaccount.com"
export PROCESS_TOKEN="dev-token"
export CLEANUP_TOKEN="dev-token"

gunicorn --bind :8080 --workers 1 --threads 8 --timeout 3600 api:app
```
