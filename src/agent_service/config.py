from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    OPENAI_API_KEY: SecretStr = SecretStr("")
    VOYAGE_API_KEY: SecretStr = SecretStr("")

    DATABASE_URL: str = "postgresql://postgres:postgres@postgres:5432/agent_db"
    REDIS_URL: str = "redis://redis:6379/0"

    MAIN_MODEL: str = "gpt-5.1-chat-latest"
    CHEAP_MODEL: str = "gpt-5-mini"
    EMBED_MODEL: str = "voyage-3"
    RERANK_MODEL: str = "rerank-2"

    RERANK_ENABLED: bool = True
    RETRIEVAL_TOP_K: int = 40
    RETRIEVAL_FINAL_K: int = 8

    CHUNK_TOKENS: int = 512
    CHUNK_OVERLAP: int = 64

    MAX_TOOL_TURNS: int = 6
    MAX_INGEST_BYTES: int = 25 * 1024 * 1024

    LOG_LEVEL: str = "INFO"


settings = Settings()
