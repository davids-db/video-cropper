#!/usr/bin/env bash
# scripts/permissions.sh
#
# Purpose (mirrors ivrit-transcriber):
# - Enable required GCP APIs
# - Create service accounts used by Cloud Run and Cloud Tasks
# - Create a GCS bucket (if missing) for outputs / uploads
# - Grant IAM roles required for Firestore + Cloud Tasks + Storage
# - Create a Cloud Tasks queue
#
# Usage:
#   chmod +x scripts/*.sh
#   ./scripts/permissions.sh

set -euo pipefail

PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
REGION="${REGION:-us-central1}"
QUEUE_NAME="${QUEUE_NAME:-video-cropper-queue}"
ENV_FILE="${ENV_FILE:-.env.cloudrun}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "❌ No project set. Run: gcloud config set project YOUR_PROJECT"
  exit 1
fi

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"

RUNTIME_SA="video-cropper-runtime-sa"
RUNTIME_SA_EMAIL="${RUNTIME_SA}@${PROJECT_ID}.iam.gserviceaccount.com"

TASKS_INVOKER_SA="video-cropper-tasks-invoker-sa"
TASKS_INVOKER_SA_EMAIL="${TASKS_INVOKER_SA}@${PROJECT_ID}.iam.gserviceaccount.com"

CLOUDTASKS_SERVICE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-cloudtasks.iam.gserviceaccount.com"
CLOUDBUILD_SERVICE_ACCOUNT="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"
COMPUTE_ENGINE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

BUCKET_NAME="${PROJECT_ID}-video-cropper-bucket"

echo "🔧 Enabling APIs..."
gcloud services enable       run.googleapis.com       cloudbuild.googleapis.com       artifactregistry.googleapis.com       firestore.googleapis.com       cloudtasks.googleapis.com       cloudscheduler.googleapis.com       storage.googleapis.com       --project "$PROJECT_ID"

echo "📦 Creating GCS bucket (if missing)..."
gsutil mb -p "$PROJECT_ID" -l "$REGION" "gs://${BUCKET_NAME}" 2>/dev/null || true
echo "✅ Bucket ready: gs://${BUCKET_NAME}"

echo "👤 Creating service accounts (if missing)..."
gcloud iam service-accounts create "$RUNTIME_SA"       --project "$PROJECT_ID"       --display-name "Video cropper Cloud Run runtime SA" 2>/dev/null || true

gcloud iam service-accounts create "$TASKS_INVOKER_SA"       --project "$PROJECT_ID"       --display-name "Cloud Tasks -> Cloud Run invoker SA" 2>/dev/null || true

echo "🔑 Grant runtime SA permissions..."

# Firestore access
gcloud projects add-iam-policy-binding "$PROJECT_ID"       --member="serviceAccount:${RUNTIME_SA_EMAIL}"       --role="roles/datastore.user" >/dev/null

# Cloud Logging
gcloud projects add-iam-policy-binding "$PROJECT_ID"       --member="serviceAccount:${RUNTIME_SA_EMAIL}"       --role="roles/logging.logWriter" >/dev/null

# Enqueue tasks
gcloud projects add-iam-policy-binding "$PROJECT_ID"       --member="serviceAccount:${RUNTIME_SA_EMAIL}"       --role="roles/cloudtasks.enqueuer" >/dev/null

echo "🪣 Grant runtime SA permission to read/write the bucket..."
gsutil iam ch "serviceAccount:${RUNTIME_SA_EMAIL}:objectAdmin" "gs://${BUCKET_NAME}" >/dev/null
gsutil iam ch "serviceAccount:${RUNTIME_SA_EMAIL}:legacyBucketReader" "gs://${BUCKET_NAME}" >/dev/null

echo "🔑 Grant Cloud Build SA permissions..."
gcloud projects add-iam-policy-binding "$PROJECT_ID"       --member="serviceAccount:${CLOUDBUILD_SERVICE_ACCOUNT}"       --role="roles/cloudbuild.builds.builder" >/dev/null

gcloud projects add-iam-policy-binding "$PROJECT_ID"       --member="serviceAccount:${CLOUDBUILD_SERVICE_ACCOUNT}"       --role="roles/artifactregistry.writer" >/dev/null

gcloud projects add-iam-policy-binding "$PROJECT_ID"       --member="serviceAccount:${CLOUDBUILD_SERVICE_ACCOUNT}"       --role="roles/storage.admin" >/dev/null

gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA_EMAIL"       --member="serviceAccount:${CLOUDBUILD_SERVICE_ACCOUNT}"       --role="roles/iam.serviceAccountUser"       --project="$PROJECT_ID" >/dev/null

gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA_EMAIL"       --member="serviceAccount:${COMPUTE_ENGINE_SA}"       --role="roles/iam.serviceAccountUser"       --project="$PROJECT_ID" >/dev/null

echo "🔐 Fix for Cloud Tasks OIDC: allow runtime SA to 'actAs' the invoker SA..."
gcloud iam service-accounts add-iam-policy-binding "$TASKS_INVOKER_SA_EMAIL"       --member="serviceAccount:${RUNTIME_SA_EMAIL}"       --role="roles/iam.serviceAccountUser"       --project="$PROJECT_ID" >/dev/null

echo "🔐 Allow Cloud Tasks service agent to mint OIDC tokens for invoker SA..."
gcloud iam service-accounts add-iam-policy-binding "$TASKS_INVOKER_SA_EMAIL"       --member="serviceAccount:${CLOUDTASKS_SERVICE_AGENT}"       --role="roles/iam.serviceAccountTokenCreator"       --project="$PROJECT_ID" >/dev/null || true

echo "👤 Grant your user account permission to impersonate the runtime SA for testing..."
CURRENT_USER="$(gcloud config get-value account)"
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA_EMAIL"       --member="user:${CURRENT_USER}"       --role="roles/iam.serviceAccountTokenCreator"       --project "$PROJECT_ID" >/dev/null 2>/dev/null || true

echo "📬 Ensure Cloud Tasks queue exists..."
gcloud tasks queues create "$QUEUE_NAME"       --location="$REGION"       --project="$PROJECT_ID" >/dev/null 2>/dev/null || true

echo
echo "✅ Base permissions done."
echo "Info:"
echo "  Runtime SA:       ${RUNTIME_SA_EMAIL}"
echo "  Tasks Invoker SA: ${TASKS_INVOKER_SA_EMAIL}"
echo "  Bucket:           gs://${BUCKET_NAME}"
echo "  Queue:            ${QUEUE_NAME} (${REGION})"

if [[ -n "${ENV_FILE}" ]]; then
  echo "🧾 Writing non-secret env file: ${ENV_FILE}"
  cat > "${ENV_FILE}" <<EOF
export PROJECT_ID="${PROJECT_ID}"
export REGION="${REGION}"
export SERVICE_NAME="video-cropper-service"
export TASKS_QUEUE="${QUEUE_NAME}"
export TASKS_INVOKER_SA_EMAIL="${TASKS_INVOKER_SA_EMAIL}"
export OUTPUT_BUCKET="${BUCKET_NAME}"
EOF
  chmod 600 "${ENV_FILE}" || true
fi
