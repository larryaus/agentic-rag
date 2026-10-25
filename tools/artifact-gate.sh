#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

npx -w infra cdk synth --quiet
artifact_path="$(.venv/bin/python tools/artifact-check.py infra/cdk.out)"
image='public.ecr.aws/lambda/python:3.12-arm64'
docker run --rm --platform linux/arm64 --network none -i \
  --entrypoint /bin/bash -w /var/task -v "$artifact_path:/var/task:ro" \
  -v "$PWD/tools/artifact-smoke.py:/tmp/artifact-smoke.py:ro" -e PYTHONPATH=/var/task \
  -e DEPLOY_TARGET=aws -e TABLE_NAME=artifact-table -e DOCS_BUCKET=artifact-documents \
  -e OIDC_ISSUER=https://issuer.invalid -e OIDC_CLIENT_ID=artifact-client \
  -e OIDC_REQUIRED_SCOPE=kb-api/access -e KNOWLEDGE_BASE_ID=artifact-kb \
  -e DATA_SOURCE_ID=artifact-ds -e CHAT_MODEL_ID=artifact-model \
  -e GUARDRAIL_ID=artifact-guard -e GUARDRAIL_VERSION=1 \
  -e AWS_ACCESS_KEY_ID=testing -e AWS_SECRET_ACCESS_KEY=testing \
  -e AWS_DEFAULT_REGION=us-east-1 -e AWS_REGION=us-east-1 \
  -e AWS_EC2_METADATA_DISABLED=true -e AWS_LWA_PORT=8080 "$image" -s <<'CONTAINER'
set -euo pipefail
python /tmp/artifact-smoke.py imports
./run.sh >/tmp/app.log 2>&1 &
app_pid=$!
smoke_pid=''
trap 'kill "$app_pid" ${smoke_pid:+"$smoke_pid"} 2>/dev/null || true; wait 2>/dev/null || true' EXIT
wait_ready() {
  local port="$1" pid="$2" logfile="$3" deadline=$((SECONDS + 90))
  while ((SECONDS < deadline)); do
    if curl --max-time 1 -sf "http://127.0.0.1:$port/healthz" >/tmp/health.json; then return; fi
    if ! kill -0 "$pid" 2>/dev/null; then cat "$logfile"; exit 1; fi
    sleep 0.25
  done
  echo "Timed out after 90 seconds waiting for uvicorn on port $port (including QEMU startup)" >&2
  cat "$logfile"
  exit 1
}
wait_ready 8080 "$app_pid" /tmp/app.log
python -c 'import json; assert json.load(open("/tmp/health.json")) == {"status":"ok"}; print("GET /healthz: 200")'
for route in / /v1/documents; do
  method=GET
  if [ "$route" = / ]; then method=POST; fi
  status="$(curl -s -o /tmp/unauthorized.json -w '%{http_code}' -X "$method" "http://127.0.0.1:8080$route")"
  test "$status" = 401
  python -c 'import json; assert json.load(open("/tmp/unauthorized.json")) == {"message":"Unauthorized"}'
  echo "$method $route: 401 JSON"
done
python /tmp/artifact-smoke.py auth
python /tmp/artifact-smoke.py serve >/tmp/smoke.log 2>&1 &
smoke_pid=$!
wait_ready 8081 "$smoke_pid" /tmp/smoke.log
python /tmp/artifact-smoke.py stream
python - <<'PY'
from kb_assistant.adapters.aws.lambda_events import ingest_handler
ingest_handler({'detail': {'bucket': {'name': 'artifact-documents'}, 'object': {'key': 'uploads/owner/id/doc.md.metadata.json', 'size': 1}}}, None)
print('Sidecar invocation: skipped without AWS calls (network disabled)')
PY
CONTAINER
printf '%s\n' 'Artifact gate passed.' 'AWS validation outstanding: Lambda Web Adapter layer, streaming transport, and IAM.'
