from functools import lru_cache

from openai import AsyncOpenAI
import voyageai

from agent_service.config import settings


@lru_cache(maxsize=1)
def get_openai() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=settings.OPENAI_API_KEY.get_secret_value())


@lru_cache(maxsize=1)
def get_voyage() -> voyageai.AsyncClient:
    return voyageai.AsyncClient(api_key=settings.VOYAGE_API_KEY.get_secret_value())
