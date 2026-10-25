from functools import cached_property
from typing import Any

from kb_assistant.core.ports import (
    BlobStore,
    ChatModel,
    Guardrail,
    Indexer,
    Retriever,
    Store,
    TokenVerifier,
)
from kb_assistant.settings import Settings


class Container:
    """Builds each adapter on first use, choosing the deployment's implementation.

    Adapter modules are imported lazily, so the Lambda package never imports the local
    dependencies and the Docker image never creates an AWS client. (Strands, which runs
    the agent loop in both, brings boto3 into the image as a dependency.)
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.local = settings.deploy_target == "local"
        self._pool: Any = None

    async def start(self) -> None:
        if not self.local:
            return
        import asyncpg
        from pgvector.asyncpg import register_vector

        from .adapters.local.pg_schema import ensure_schema

        url = self.settings.require("database_url")
        # The vector type must exist before the pool registers its codec.
        connection = await asyncpg.connect(url)
        try:
            await ensure_schema(
                connection, self.settings.embedding_model, self.settings.embedding_dimension
            )
        finally:
            await connection.close()
        self._pool = await asyncpg.create_pool(url, init=register_vector, min_size=1, max_size=10)
        self.worker.start()

    async def stop(self) -> None:
        if "worker" in vars(self):
            await self.worker.stop()
        if self._pool is not None:
            await self._pool.close()

    def initialize_http(self) -> None:
        properties = {"store", "verifier"}
        if "chat" in self.settings.route_names:
            properties.update({"model", "retriever", "guardrail"})
        if self.local or self.settings.route_names & {"uploads", "documents"}:
            properties.add("blobs")
        for name in sorted(properties):
            getattr(self, name)

    @property
    def pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("The database pool starts with the app")
        return self._pool

    @cached_property
    def store(self) -> Store:
        if self.local:
            from .adapters.local.pg_store import PgStore

            return PgStore(self.pool)
        from .adapters.aws.ddb_store import DdbStore

        return DdbStore(self.settings.require("table_name"))

    @cached_property
    def blobs(self) -> BlobStore:
        if self.local:
            from pathlib import Path

            from .adapters.local.fs_blobs import FsBlobs, load_secret

            root = Path(self.settings.blob_root)
            return FsBlobs(
                root,
                self.settings.public_base_url,
                load_secret(root, self.settings.blob_signing_secret),
                on_upload=lambda key: self.worker.notify(key),
            )
        from .adapters.aws.s3_blobs import S3Blobs

        return S3Blobs(self.settings.require("docs_bucket"))

    @cached_property
    def openai_client(self) -> Any:
        from openai import AsyncOpenAI

        return AsyncOpenAI(base_url=self.settings.llm_base_url, api_key=self.settings.llm_key)

    @cached_property
    def embedder(self) -> Any:
        from openai import AsyncOpenAI

        from .adapters.local.openai_embed import OpenAIEmbedder

        settings = self.settings
        client = (
            self.openai_client
            if not settings.embedding_base_url and not settings.embedding_api_key
            else AsyncOpenAI(
                base_url=settings.embedding_base_url or settings.llm_base_url,
                api_key=settings.embedding_api_key or settings.llm_key,
            )
        )
        return OpenAIEmbedder(
            settings.embedding_model, settings.embedding_dimension, client=client
        )

    @cached_property
    def model(self) -> ChatModel:
        if self.local:
            from .adapters.local.openai_chat import OpenAIChat

            return OpenAIChat(self.settings.llm_model, client=self.openai_client)
        from .adapters.aws.bedrock_chat import BedrockChat

        return BedrockChat(
            self.settings.require("chat_model_id"),
            self.settings.require("guardrail_id"),
            self.settings.require("guardrail_version"),
        )

    @cached_property
    def retriever(self) -> Retriever:
        if self.local:
            from .adapters.local.pg_retriever import PgRetriever

            return PgRetriever(self.pool, self.embedder)
        from .adapters.aws.kb_retriever import KbRetriever

        return KbRetriever(self.settings.require("knowledge_base_id"))

    @cached_property
    def indexer(self) -> Indexer:
        if self.local:
            raise RuntimeError("The local deployment ingests in-process; see Container.worker")
        from .adapters.aws.kb_indexer import KbIndexer

        return KbIndexer(
            self.settings.require("knowledge_base_id"),
            self.settings.require("data_source_id"),
            self.settings.require("docs_bucket"),
        )

    @cached_property
    def worker(self) -> Any:
        from .adapters.local.worker import Worker

        settings = self.settings
        return Worker(
            self.pool,
            self.blobs,  # type: ignore[arg-type]
            self.embedder,
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
            lease_seconds=settings.ingest_lease_seconds,
            sweep_seconds=settings.worker_sweep_seconds,
            abandoned_upload_minutes=settings.abandoned_upload_minutes,
        )

    @cached_property
    def guardrail(self) -> Guardrail:
        if self.local:
            from .adapters.local.guardrail import LocalGuardrail

            return LocalGuardrail(
                self.model, check_input=self.settings.guardrail_input_check == "model"
            )
        from .core.guard_stream import PassThroughGuardrail

        return PassThroughGuardrail()

    @cached_property
    def verifier(self) -> TokenVerifier:
        from .adapters.oidc import OidcVerifier

        issuer = self.settings.require("oidc_issuer")
        return OidcVerifier(
            issuer,
            self.settings.oidc_jwks_url or f"{issuer}/.well-known/jwks.json",
            self.settings.require("oidc_client_id"),
            self.settings.oidc_required_scope,
        )
