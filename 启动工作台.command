#!/bin/bash
set -e
cd "$(dirname "$0")"
if curl -fsS --max-time 2 "http://127.0.0.1:${PROTEIN_STUDIO_PORT:-8765}/api/health" >/dev/null 2>&1; then
  open "http://127.0.0.1:${PROTEIN_STUDIO_PORT:-8765}"
  exit 0
fi
(sleep 2; open "http://127.0.0.1:${PROTEIN_STUDIO_PORT:-8765}") &
./start.sh
