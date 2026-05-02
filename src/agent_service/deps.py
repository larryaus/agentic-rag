from functools import lru_cache

import anthropic
import voyageai

from agent_service.config import settings


@lru_cache(maxsize=1)
def get_anthropic() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY.get_secret_value())


@lru_cache(maxsize=1)
def get_voyage() -> voyageai.AsyncClient:
    return voyageai.AsyncClient(api_key=settings.VOYAGE_API_KEY.get_secret_value())
