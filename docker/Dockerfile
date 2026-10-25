# The local deployment: the React frontend built for localhost, served by the Python app.
# Build from the repository root: docker compose build (or docker build -f docker/Dockerfile .)

FROM node:24-slim AS frontend
WORKDIR /src
COPY package.json package-lock.json tsconfig.base.json tsconfig.json ./
COPY shared/package.json shared/
COPY frontend/package.json frontend/
COPY infra/package.json infra/
RUN npm ci --workspace frontend --workspace shared --include-workspace-root --no-audit --no-fund
COPY shared/ shared/
COPY frontend/ frontend/
# Keycloak is reached by the browser on localhost:8080 and the API on localhost:8000.
ARG VITE_OIDC_ISSUER=http://localhost:8080/realms/kb
ARG VITE_OIDC_CLIENT_ID=kb-web
ARG VITE_OIDC_AUTHORIZE_URL=http://localhost:8080/realms/kb/protocol/openid-connect/auth
ARG VITE_OIDC_TOKEN_URL=http://localhost:8080/realms/kb/protocol/openid-connect/token
ARG VITE_OIDC_JWKS_URL=http://localhost:8080/realms/kb/protocol/openid-connect/certs
ARG VITE_OIDC_SCOPE="openid profile email kb-api"
ARG VITE_OIDC_LOGOUT_URL=http://localhost:8080/realms/kb/protocol/openid-connect/logout
ARG VITE_OIDC_LOGOUT_MODE=oidc
ARG VITE_API_URL=http://localhost:8000
ARG VITE_CHAT_URL=http://localhost:8000/v1/chat
ENV VITE_OIDC_ISSUER=$VITE_OIDC_ISSUER \
    VITE_OIDC_CLIENT_ID=$VITE_OIDC_CLIENT_ID \
    VITE_OIDC_AUTHORIZE_URL=$VITE_OIDC_AUTHORIZE_URL \
    VITE_OIDC_TOKEN_URL=$VITE_OIDC_TOKEN_URL \
    VITE_OIDC_JWKS_URL=$VITE_OIDC_JWKS_URL \
    VITE_OIDC_SCOPE=$VITE_OIDC_SCOPE \
    VITE_OIDC_LOGOUT_URL=$VITE_OIDC_LOGOUT_URL \
    VITE_OIDC_LOGOUT_MODE=$VITE_OIDC_LOGOUT_MODE \
    VITE_API_URL=$VITE_API_URL \
    VITE_CHAT_URL=$VITE_CHAT_URL
RUN npm run -w frontend build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# Fully pinned: the local lock is resolved against the Lambda lock, so the packages the
# two deployments share are the same versions.
COPY backend/requirements-local.lock backend/
RUN pip install --no-cache-dir -r backend/requirements-local.lock
COPY backend/ backend/
RUN pip install --no-cache-dir --no-deps ./backend
COPY --from=frontend /src/frontend/dist /app/frontend
RUN useradd --create-home --uid 10001 app && mkdir -p /data/blobs && chown app /data/blobs
USER app
ENV DEPLOY_TARGET=local FRONTEND_DIST_DIR=/app/frontend BLOB_ROOT=/data/blobs
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"
CMD ["uvicorn", "kb_assistant.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
