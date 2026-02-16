#!/usr/bin/env bash
# scripts/scheduler.sh
#
# Purpose:
# - Create or update a Cloud Scheduler job that calls:
#     POST /cleanup
#   daily, to delete Firestore job docs older than RETENTION_DAYS.
#
# Auth:
# - Uses header X-Cleanup-Token (CLEANUP_TOKEN env var)

set -euo pipefail

PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-video-cropper-service}"
ENV_FILE="${ENV_FILE:-.env.cloudrun}"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
fi

if [[ -z "${PROJECT_ID}" ]]; then
  echo "❌ No project set."
  exit 1
fi

if [[ -z "${CLEANUP_TOKEN:-}" ]]; then
  echo "❌ Set CLEANUP_TOKEN in your shell (value printed by deploy.sh)."
  exit 1
fi

SERVICE_URL="${SERVICE_URL:-$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --format='value(status.url)' --project "$PROJECT_ID")}"

JOB_NAME="${SERVICE_NAME}-cleanup"
SCHEDULE="15 3 * * *"  # daily at 03:15 UTC

echo "🕒 Creating/updating scheduler job: ${JOB_NAME}"
gcloud scheduler jobs create http "$JOB_NAME"       --project "$PROJECT_ID"       --location "$REGION"       --schedule "$SCHEDULE"       --uri "${SERVICE_URL}/cleanup"       --http-method POST       --headers "X-Cleanup-Token=${CLEANUP_TOKEN}"       --message-body "{}"       2>/dev/null ||     gcloud scheduler jobs update http "$JOB_NAME"       --project "$PROJECT_ID"       --location "$REGION"       --schedule "$SCHEDULE"       --uri "${SERVICE_URL}/cleanup"       --http-method POST       --headers "X-Cleanup-Token=${CLEANUP_TOKEN}"       --message-body "{}"

echo "✅ Scheduler configured: ${JOB_NAME}"
echo "Calls: ${SERVICE_URL}/cleanup"
