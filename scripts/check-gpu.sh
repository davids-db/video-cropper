#!/usr/bin/env bash
# scripts/check-gpu.sh
#
# Verifies GPU access on the deployed Cloud Run service.
#
# Checks:
#   1. Service configuration (gcloud) — confirms --gpu 1 is set
#   2. /gpu endpoint — confirms CUDA is visible inside the container
#
# Usage:
#   ./scripts/check-gpu.sh
#   SERVICE_URL=https://... ./scripts/check-gpu.sh   # override URL

set -euo pipefail

PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
REGION="${REGION:-europe-west1}"
SERVICE_NAME="${SERVICE_NAME:-video-cropper-service}"
SERVICE_URL="${SERVICE_URL:-}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "❌ No project set. Run: gcloud config set project YOUR_PROJECT"
  exit 1
fi

# ── 1. Check service config ──────────────────────────────────────────────────
echo "🔍 Checking Cloud Run service configuration..."
GPU_COUNT=$(gcloud run services describe "$SERVICE_NAME" \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --format='value(spec.template.metadata.annotations."run.googleapis.com/gpu-type")' 2>/dev/null || true)

# Older gcloud versions use a different annotation path; try the resource format too.
if [[ -z "$GPU_COUNT" ]]; then
  GPU_COUNT=$(gcloud run services describe "$SERVICE_NAME" \
    --region "$REGION" \
    --project "$PROJECT_ID" \
    --format='json' \
    | python3 -c "
import json, sys
svc = json.load(sys.stdin)
ann = svc.get('spec',{}).get('template',{}).get('metadata',{}).get('annotations',{})
gpu = ann.get('run.googleapis.com/accelerator','') or ann.get('run.googleapis.com/gpu-type','')
print(gpu)
" 2>/dev/null || true)
fi

if [[ -n "$GPU_COUNT" ]]; then
  echo "  ✅ GPU configured: ${GPU_COUNT}"
else
  echo "  ⚠️  No GPU annotation found — service may be CPU-only or not yet deployed with --gpu 1"
fi

# ── 2. Resolve service URL ───────────────────────────────────────────────────
if [[ -z "${SERVICE_URL}" ]]; then
  SERVICE_URL="$(gcloud run services describe "$SERVICE_NAME" \
    --region "$REGION" \
    --format='value(status.url)' \
    --project "$PROJECT_ID")"
fi
echo "  🌐 Service URL: ${SERVICE_URL}"

# ── 3. Hit /gpu endpoint ─────────────────────────────────────────────────────
echo ""
echo "🔍 Querying /gpu endpoint..."
TOKEN="$(gcloud auth print-identity-token)"
RESPONSE=$(curl -sf "${SERVICE_URL}/gpu" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Accept: application/json" || true)

if [[ -z "$RESPONSE" ]]; then
  echo "  ❌ No response from ${SERVICE_URL}/gpu — is the service running?"
  exit 1
fi

echo "  Response: ${RESPONSE}"

CUDA_OK=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('cuda_available','false'))" 2>/dev/null || echo "false")

if [[ "$CUDA_OK" == "True" ]]; then
  DEVICE=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('device_name','unknown'))" 2>/dev/null || echo "unknown")
  MEM=$(echo "$RESPONSE"    | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('memory_total_mb','?'))" 2>/dev/null || echo "?")
  echo ""
  echo "  ✅ CUDA is available"
  echo "     Device : ${DEVICE}"
  echo "     VRAM   : ${MEM} MB"
else
  echo ""
  echo "  ❌ CUDA is NOT available inside the container."
  echo "     - Confirm the service was deployed with --gpu 1"
  echo "     - Confirm the CUDA torch wheel was installed (whl/cu121)"
  echo "     - Check build logs: gcloud builds list --region=${REGION} --limit=1"
  exit 1
fi
