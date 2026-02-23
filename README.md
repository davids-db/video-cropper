# video-cropper-service

Person-detection-based video cropper as a Cloud Run service (GPU-accelerated).

**Async job model**: `POST /submit` enqueues a Cloud Task that calls `POST /process`, while job state/results live in Firestore. Output is H.264 MP4 with audio, uploaded back to GCS.

## Architecture

```
Client → POST /submit → Firestore (queued) + Cloud Task
                                    ↓
                        Cloud Tasks → POST /process
                                    ↓
                        YOLOv8 person detection (GPU)
                        EMA crop window smoother
                        ffmpeg H.264 encode + audio mux
                                    ↓
                        GCS output + Firestore (done)
                                    ↓
Client ← GET /status/<job_id> ← Firestore
```

## HTTP API

Base URL: your Cloud Run service URL.

### `GET /health`
Returns `{"ok": true}`.

### `GET /gpu`
Returns CUDA device info and live VRAM usage:
```json
{
  "cuda_available": true,
  "device_name": "NVIDIA L4",
  "memory_total_mb": 22491,
  "memory_used_mb": 312,
  "memory_reserved_mb": 512,
  "memory_free_mb": 21979
}
```

### `POST /submit`
Request:
```json
{"uri": "gs://bucket/path/video.mp4"}
```
Response (202):
```json
{"job_id": "<uuid>", "status": "queued"}
```
URI schemes supported: `gs://`, `http://`, `https://`.

### `GET /status/<job_id>`
- queued/processing:
  ```json
  {"job_id": "...", "status": "processing", "created_at_ts": "...", "updated_at_ts": "..."}
  ```
- done:
  ```json
  {"job_id": "...", "status": "done", "result": {"output_uri": "gs://.../video_cropped.mp4"}}
  ```
- failed:
  ```json
  {"job_id": "...", "status": "failed", "error": "..."}
  ```

### `POST /process` (internal)
Cloud Tasks worker endpoint. Protected by IAM OIDC + `X-Process-Token` header.

### `POST /cleanup` (internal)
Cloud Scheduler endpoint. Protected by `X-Cleanup-Token` header.

## Input / Output

- Input: `gs://bucket/path.mp4` or `http(s)://...`
- Output:
  - `gs://` inputs → same bucket, `_cropped` suffix (e.g. `test.mp4` → `test_cropped.mp4`)
  - `http(s)://` inputs → `gs://$OUTPUT_BUCKET/<basename>_cropped.mp4`

## Performance

| Mode | Batch size | Speed | ~35k frame video |
|------|-----------|-------|-----------------|
| CPU only | 4 | ~8 fps | ~60 min |
| GPU (NVIDIA L4) | 32 | ~150-200 fps | ~3-4 min |

GPU uses fp16 inference + threaded frame pre-fetching so the GPU stays fed while OpenCV decodes the next batch.

## Environment variables

Required:
- `PROJECT_ID`
- `SERVICE_URL`
- `TASKS_INVOKER_SA_EMAIL`
- `PROCESS_TOKEN`
- `CLEANUP_TOKEN`

Optional:
- `REGION` (default `europe-west1`)
- `TASKS_QUEUE` (default `video-cropper-queue`)
- `FIRESTORE_COLLECTION` (default `video_crop_jobs`)
- `RETENTION_DAYS` (default `14`)
- `STALLED_MINUTES` (default `45` — jobs stuck in `processing` are marked `failed` after this many minutes)
- `USE_GPU` (default `1` — set to `0` to deploy CPU-only while GPU quota is pending)
- `OUTPUT_BUCKET` (required for http(s) inputs; default `<PROJECT_ID>-video-cropper-eu-bucket`)

Cropper tuning:
- `MODEL_NAME` (default `yolov8n.pt`)
- `CONF` (default `0.25`)
- `IOU` (default `0.5`)
- `PADDING_RATIO` (default `0.12`)
- `MIN_CROP_RATIO` (default `0.35`)
- `SMOOTH_ALPHA` (default `0.85`)
- `KEEP_ASPECT` (default `1`)
- `DRAW_TIMESTAMP` (default `1`)
- `DETECT_BATCH_SIZE` (default `32` for GPU; use `4` for CPU)

## Deployment on GCP

### 1) Set project
```bash
gcloud config set project YOUR_PROJECT_ID
```

### 2) Set up permissions (run once per region)
```bash
chmod +x scripts/*.sh
./scripts/permissions.sh
```
Creates service accounts, GCS bucket, Cloud Tasks queue in `europe-west1`.

### 3) Initialize Firestore (first time only)
```bash
gcloud firestore databases create --location=europe-west1 --project=YOUR_PROJECT_ID
```

### 4) Deploy Cloud Run with GPU
```bash
USE_GPU=1 ENV_FILE=.env.cloudrun ./scripts/deploy.sh
```

CPU-only fallback (while GPU quota is pending):
```bash
USE_GPU=0 DETECT_BATCH_SIZE=4 ENV_FILE=.env.cloudrun ./scripts/deploy.sh
```

### 5) Verify GPU
```bash
./scripts/check-gpu.sh
```

### 6) Schedule cleanup
```bash
source .env.cloudrun
./scripts/scheduler.sh
```

## Submitting a job

```bash
source .env.cloudrun
TOKEN=$(gcloud auth print-identity-token)

# Submit
JOB=$(curl -s -X POST "${SERVICE_URL}/submit" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"uri":"gs://YOUR_BUCKET/video.mp4"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
echo "Job ID: $JOB"

# Poll status
curl -s "${SERVICE_URL}/status/${JOB}" -H "Authorization: Bearer ${TOKEN}"

# Download result
gsutil cp gs://YOUR_BUCKET/video_cropped.mp4 .
```

## Viewing logs

```bash
# Job progress and completion
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="video-cropper-service"' \
  --limit=50 --order=desc \
  --format='table(timestamp,severity,jsonPayload.message,textPayload)' \
  --project=$(gcloud config get-value project)

# Errors only
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="video-cropper-service" AND severity>=ERROR' \
  --limit=20 --order=desc \
  --format='table(timestamp,severity,jsonPayload.message,jsonPayload.exc)' \
  --project=$(gcloud config get-value project)

# Cloud Build logs (container build failures)
BUILD_ID=$(gcloud builds list --region=europe-west1 --limit=1 --format='value(id)')
gcloud builds log "$BUILD_ID" --region=europe-west1
```

### Key log events

| Event | What it means |
|---|---|
| `run_start` | Worker picked up a job, downloading video |
| `video_info` | Input opened; shows fps/dimensions/frame count |
| `loading_model` | YOLO model loading (first job per container only) |
| `processed_frames n=64` | Progress heartbeat every 64 frames |
| `run_complete elapsed_s=...` | Job finished; total wall time |
| `job_complete` | Firestore updated to `done`, output URI confirmed |
| `job_already_active` | Idempotency guard fired — task was retried but job already running |
| `job_failed_processing_error` | Known error (bad video, URI, etc.) |
| `job_failed_unexpected` | Unhandled exception — check `exc` field for traceback |

## Check job status via Firestore (when service is busy)

```bash
curl -s \
  "https://firestore.googleapis.com/v1/projects/YOUR_PROJECT/databases/(default)/documents/video_crop_jobs/JOB_ID" \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
fields = data.get('fields', {})
print('status:', fields.get('status', {}).get('stringValue'))
result = fields.get('result', {}).get('mapValue', {}).get('fields', {})
print('output:', result.get('output_uri', {}).get('stringValue', 'N/A'))
"
```

## Local dev

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export PROJECT_ID="your-project"
export SERVICE_URL="http://localhost:8080"
export TASKS_INVOKER_SA_EMAIL="your-sa@project.iam.gserviceaccount.com"
export PROCESS_TOKEN="dev-token"
export CLEANUP_TOKEN="dev-token"

gunicorn --bind :8080 --workers 1 --threads 8 --timeout 3600 api:app
```
