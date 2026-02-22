#!/usr/bin/env bash
# scripts/deploy.sh - Deploys the video-cropper-service
#
# Purpose:
# - Deploy the Cloud Run service from source (Cloud Build)
# - Set required environment variables
# - Wire Cloud Tasks -> Cloud Run invocation:
#     - grant roles/run.invoker to TASKS_INVOKER_SA on the service
#     - allow Cloud Tasks service agent to mint tokens as TASKS_INVOKER_SA
#
# Usage:
#   ENV_FILE=.env.cloudrun ./scripts/deploy.sh
#
# Outputs:
# - SERVICE_URL
# - CLEANUP_TOKEN (save this to configure scheduler)

set -euo pipefail

PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
REGION="${REGION:-europe-west1}"
SERVICE_NAME="${SERVICE_NAME:-video-cropper-service}"
QUEUE_NAME="${QUEUE_NAME:-video-cropper-queue}"
ENV_FILE="${ENV_FILE:-}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "❌ No project set. Run: gcloud config set project YOUR_PROJECT"
  exit 1
fi

RUNTIME_SA_EMAIL="video-cropper-runtime-sa@${PROJECT_ID}.iam.gserviceaccount.com"
TASKS_INVOKER_SA_EMAIL="video-cropper-tasks-invoker-sa@${PROJECT_ID}.iam.gserviceaccount.com"

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
CLOUDTASKS_SERVICE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-cloudtasks.iam.gserviceaccount.com"

# Shared secrets (defense-in-depth)
if [[ -z "${PROCESS_TOKEN:-}" ]]; then
  PROCESS_TOKEN="$(openssl rand -hex 16)"
  echo "🔐 Generated PROCESS_TOKEN=${PROCESS_TOKEN}"
fi

if [[ -z "${CLEANUP_TOKEN:-}" ]]; then
  CLEANUP_TOKEN="$(openssl rand -hex 16)"
  echo "🔐 Generated CLEANUP_TOKEN=${CLEANUP_TOKEN}"
fi

RETENTION_DAYS="${RETENTION_DAYS:-14}"
STALLED_MINUTES="${STALLED_MINUTES:-45}"
MAX_INSTANCES="${MAX_INSTANCES:-1}"

# Cropper tuning
MODEL_NAME="${MODEL_NAME:-yolov8n.pt}"
CONF="${CONF:-0.25}"
IOU="${IOU:-0.5}"
PADDING_RATIO="${PADDING_RATIO:-0.12}"
MIN_CROP_RATIO="${MIN_CROP_RATIO:-0.35}"
SMOOTH_ALPHA="${SMOOTH_ALPHA:-0.85}"
KEEP_ASPECT="${KEEP_ASPECT:-1}"
DRAW_TIMESTAMP="${DRAW_TIMESTAMP:-1}"
DETECT_BATCH_SIZE="${DETECT_BATCH_SIZE:-4}"  # 8 triggers NMS timeout on CPU; use 8+ only with GPU
OUTPUT_BUCKET="${OUTPUT_BUCKET:-${PROJECT_ID}-video-cropper-bucket}"
USE_GPU="${USE_GPU:-1}"  # set USE_GPU=0 to deploy without GPU (e.g. while quota is pending)

BUILD_ARGS=()
if [[ "${PRECACHE_YOLO:-0}" == "1" ]]; then
  BUILD_ARGS+=("--build-arg" "PRECACHE_YOLO=1")
  BUILD_ARGS+=("--build-arg" "MODEL_NAME=${MODEL_NAME}")
fi

# GPU flags: only added when USE_GPU=1
GPU_ARGS=()
if [[ "${USE_GPU}" == "1" ]]; then
  GPU_ARGS+=("--gpu" "1" "--no-cpu-throttling")
  MEMORY="16Gi"
  echo "🖥️  Deploying WITH GPU (NVIDIA L4)"
else
  MEMORY="8Gi"
  echo "🖥️  Deploying WITHOUT GPU (CPU only)"
fi

echo "🚀 Deploying Cloud Run service..."
gcloud run deploy "$SERVICE_NAME"       --source .       --region "$REGION"       --platform managed       --service-account "$RUNTIME_SA_EMAIL"       --memory "${MEMORY}"       --cpu 4       "${GPU_ARGS[@]}"       --min-instances 0       --timeout 3600       --max-instances "${MAX_INSTANCES}"       --concurrency 8       "${BUILD_ARGS[@]}"       --set-env-vars "PROJECT_ID=${PROJECT_ID},REGION=${REGION},TASKS_QUEUE=${QUEUE_NAME},PROCESS_TOKEN=${PROCESS_TOKEN},CLEANUP_TOKEN=${CLEANUP_TOKEN},RETENTION_DAYS=${RETENTION_DAYS},STALLED_MINUTES=${STALLED_MINUTES},TASKS_INVOKER_SA_EMAIL=${TASKS_INVOKER_SA_EMAIL},MODEL_NAME=${MODEL_NAME},CONF=${CONF},IOU=${IOU},PADDING_RATIO=${PADDING_RATIO},MIN_CROP_RATIO=${MIN_CROP_RATIO},SMOOTH_ALPHA=${SMOOTH_ALPHA},KEEP_ASPECT=${KEEP_ASPECT},DRAW_TIMESTAMP=${DRAW_TIMESTAMP},DETECT_BATCH_SIZE=${DETECT_BATCH_SIZE},OUTPUT_BUCKET=${OUTPUT_BUCKET}"       --project "$PROJECT_ID"

SERVICE_URL="$(gcloud run services describe "$SERVICE_NAME"       --region "$REGION"       --format='value(status.url)'       --project "$PROJECT_ID")"

echo "🌐 Service URL: ${SERVICE_URL}"

echo "🔁 Setting SERVICE_URL env var..."
gcloud run services update "$SERVICE_NAME"       --region "$REGION"       --update-env-vars "SERVICE_URL=${SERVICE_URL}"       --project "$PROJECT_ID"

echo "🔐 Allow invoker SA to call Cloud Run..."
gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
  --region "$REGION" \
  --member="serviceAccount:${TASKS_INVOKER_SA_EMAIL}" \
  --role="roles/run.invoker" \
  --project "$PROJECT_ID"

# Attempt to allow unauthenticated access (will be silently skipped if the GCP
# org policy constraints/iam.allowedPolicyMemberDomains blocks allUsers).
# If it fails, callers must include: -H "Authorization: Bearer $(gcloud auth print-identity-token)"
echo "🌍 Attempting unauthenticated public access (may be blocked by org policy)..."
gcloud beta run services add-iam-policy-binding "$SERVICE_NAME" \
  --region "$REGION" \
  --member="allUsers" \
  --role="roles/run.invoker" \
  --project "$PROJECT_ID" 2>/dev/null \
  || echo "⚠️  allUsers binding blocked by org policy — use Bearer token auth instead."

echo "🔐 Allow Cloud Tasks service agent to mint tokens as invoker SA..."
gcloud iam service-accounts add-iam-policy-binding "$TASKS_INVOKER_SA_EMAIL"       --member="serviceAccount:${CLOUDTASKS_SERVICE_AGENT}"       --role="roles/iam.serviceAccountTokenCreator"       --project "$PROJECT_ID"

echo
echo "✅ Deploy complete."
echo "Export these for scheduler setup:"
echo "  export SERVICE_URL=${SERVICE_URL}"
echo "  export CLEANUP_TOKEN=${CLEANUP_TOKEN}"
echo

if [[ -n "${ENV_FILE}" ]]; then
  echo "🧾 Writing env file: ${ENV_FILE}"
  cat > "${ENV_FILE}" <<EOF
export PROJECT_ID="${PROJECT_ID}"
export REGION="${REGION}"
export SERVICE_NAME="${SERVICE_NAME}"
export TASKS_QUEUE="${QUEUE_NAME}"
export SERVICE_URL="${SERVICE_URL}"
export TASKS_INVOKER_SA_EMAIL="${TASKS_INVOKER_SA_EMAIL}"
export PROCESS_TOKEN="${PROCESS_TOKEN}"
export CLEANUP_TOKEN="${CLEANUP_TOKEN}"
export MODEL_NAME="${MODEL_NAME}"
export CONF="${CONF}"
export IOU="${IOU}"
export PADDING_RATIO="${PADDING_RATIO}"
export MIN_CROP_RATIO="${MIN_CROP_RATIO}"
export SMOOTH_ALPHA="${SMOOTH_ALPHA}"
export KEEP_ASPECT="${KEEP_ASPECT}"
export DRAW_TIMESTAMP="${DRAW_TIMESTAMP}"
export OUTPUT_BUCKET="${OUTPUT_BUCKET}"
export STALLED_MINUTES="${STALLED_MINUTES}"
EOF
  chmod 600 "${ENV_FILE}" || true
fi

echo "Test submit:"
cat <<EOF
  TOKEN=\$(gcloud auth print-identity-token)
  curl -s -X POST ${SERVICE_URL}/submit \\
    -H "Authorization: Bearer \${TOKEN}" \\
    -H 'Content-Type: application/json' \\
    -d '{"uri":"gs://${OUTPUT_BUCKET}/file.mp4"}'
EOF
