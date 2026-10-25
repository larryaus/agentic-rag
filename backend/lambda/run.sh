#!/bin/sh
set -eu
exec python -m uvicorn kb_assistant.app:create_app --factory --host 127.0.0.1 --port "${AWS_LWA_PORT:-${PORT:-8080}}" --no-access-log
