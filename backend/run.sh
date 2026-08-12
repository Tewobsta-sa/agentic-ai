#!/usr/bin/env bash
# Runs the backend API on :8000. The frontend is mounted as static files by
# app.py, so once this is running, just open http://localhost:8000 .
set -e
cd "$(dirname "$0")"
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
