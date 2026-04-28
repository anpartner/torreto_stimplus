#!/usr/bin/env bash
set -euo pipefail

API_BASE="${API_BASE:-http://127.0.0.1:8000}"

curl -sS -X POST "${API_BASE}/api/v1/catalog/reindex" \
  -H "Content-Type: application/json" \
  -d '{"source":"akeneo","sync_mode":"delta"}'
