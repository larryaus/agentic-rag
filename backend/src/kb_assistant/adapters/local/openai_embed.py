from typing import Any

from openai import AsyncOpenAI

BATCH = 64


class EmbeddingError(RuntimeError):
    pass


class OpenAIEmbedder:
    """Any OpenAI-compatible /embeddings endpoint: OpenAI, Ollama and others."""

    def __init__(self, model: str, dimension: int, *, client: Any) -> None:
        self.model = model
        self.dimension = dimension
        self.client: AsyncOpenAI = client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH):
            batch = texts[start : start + BATCH]
            response = await self.client.embeddings.create(model=self.model, input=batch)
            data = sorted(response.data, key=lambda item: item.index)
            if len(data) != len(batch):
                raise EmbeddingError(
                    f"Embedding endpoint returned {len(data)} vectors for {len(batch)} inputs"
                )
            for item in data:
                if len(item.embedding) != self.dimension:
                    raise EmbeddingError(
                        f"Embedding model {self.model} returned {len(item.embedding)} "
                        f"dimensions; EMBEDDING_DIMENSION is {self.dimension}"
                    )
                vectors.append(list(item.embedding))
        return vectors
