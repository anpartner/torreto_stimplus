#!/usr/bin/env bash
set -euo pipefail

API_BASE="${API_BASE:-http://127.0.0.1:8000}"
QUERY="${1:-iphone}"
LIMIT="${LIMIT:-6}"
SESSION_ID="${SESSION_ID:-script-session}"
VISITOR_ID="${VISITOR_ID:-script-visitor}"

curl -sS -X POST "${API_BASE}/api/v1/search" \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"${QUERY}\",\"limit\":${LIMIT},\"session_id\":\"${SESSION_ID}\",\"visitor_id\":\"${VISITOR_ID}\",\"reset_context\":false}"
