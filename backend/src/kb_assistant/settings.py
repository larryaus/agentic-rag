from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    deploy_target: Literal["aws", "local"] = "aws"
    kb_routes: str = "chat,uploads,documents,sessions"
    table_name: str = ""
    docs_bucket: str = ""
    knowledge_base_id: str = ""
    data_source_id: str = ""
    chat_model_id: str = ""
    guardrail_id: str = ""
    guardrail_version: str = ""
    oidc_issuer: str = ""
    oidc_jwks_url: str = ""
    oidc_client_id: str = ""
    oidc_required_scope: str = ""
    cors_allow_origins: str = ""
    retrieval_top_k: int = Field(default=8, gt=0)
    max_tool_iterations: int = Field(default=6, gt=0)
    max_history_messages: int = Field(default=20, gt=0)
    session_ttl_days: int = Field(default=90, gt=0)
    max_upload_bytes: int = Field(default=26_214_400, gt=0)
    abandoned_upload_minutes: int = Field(default=10, gt=0)
    chat_turn_deadline_seconds: float = Field(default=280, gt=0)
    metrics_namespace: str = "KbAssistant"
    # Set by Lambda when active tracing is on; names the X-Ray daemon's UDP address.
    aws_xray_daemon_address: str = ""
    # Local deployment only. AWS reads none of these.
    database_url: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4.1-mini"
    llm_max_context_chars: int | None = Field(default=None, gt=0)
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int = Field(default=1536, ge=1, le=2000)
    guardrail_input_check: Literal["model", "off"] = "model"
    blob_root: str = "/data/blobs"
    blob_signing_secret: str = ""
    public_base_url: str = "http://localhost:8000"
    frontend_dist_dir: str = ""
    worker_sweep_seconds: float = Field(default=30, gt=0)
    ingest_lease_seconds: float = Field(default=300, gt=0)

    @field_validator("kb_routes")
    @classmethod
    def valid_routes(cls, value: str) -> str:
        names = [name.strip() for name in value.split(",")]
        if not set(names) <= {"chat", "uploads", "documents", "sessions"}:
            raise ValueError("KB_ROUTES must contain chat, uploads, documents or sessions")
        return ",".join(dict.fromkeys(names))

    @property
    def route_names(self) -> set[str]:
        return set(self.kb_routes.split(","))

    @property
    def chat_model(self) -> str:
        """The model that answers chat turns in this deployment."""
        return self.chat_model_id if self.deploy_target == "aws" else self.llm_model

    @property
    def llm_key(self) -> str:
        # Ollama and other local servers accept any key; hosted endpoints need a real one.
        from urllib.parse import urlsplit

        host = urlsplit(self.llm_base_url).hostname or ""
        if self.llm_api_key:
            return self.llm_api_key
        if host in {"ollama", "localhost", "127.0.0.1", "host.docker.internal"}:
            return "local"
        raise RuntimeError("Missing required environment variable: LLM_API_KEY")

    def require(self, name: str) -> str:
        value = getattr(self, name)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"Missing required environment variable: {name.upper()}")
        return value
