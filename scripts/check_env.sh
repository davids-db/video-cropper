#!/usr/bin/env bash
# scripts/check_env.sh
#
# Validates required environment variables for the service.

set -euo pipefail

missing=()

required=(
  PROJECT_ID
  SERVICE_URL
  TASKS_INVOKER_SA_EMAIL
  PROCESS_TOKEN
  CLEANUP_TOKEN
)

for var in "${required[@]}"; do
  if [[ -z "${!var:-}" ]]; then
    missing+=("$var")
  fi
done

if (( ${#missing[@]} > 0 )); then
  echo "❌ Missing environment variables:"
  for var in "${missing[@]}"; do
    echo "  - ${var}"
  done
  echo
  echo "Tip: source .env.cloudrun or export the variables above."
  exit 1
fi

echo "✅ All required environment variables are set."
