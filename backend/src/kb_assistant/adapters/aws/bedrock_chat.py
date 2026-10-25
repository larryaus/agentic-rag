import threading
from collections.abc import AsyncGenerator
from typing import Any

from strands.models.bedrock import BedrockModel
from strands.types.streaming import StreamEvent

from kb_assistant.errors import ModelStreamError, error_message

MAX_TOKENS = 2048


class BedrockChat(BedrockModel):
    """Bedrock ConverseStream through Strands, with the guardrail on every model call.

    Only the newest question is marked for the guardrail, so history and retrieved text
    are not checked, or billed, again on every call of the tool loop. Streaming uses the
    guardrail's synchronous mode, which masking requires.
    """

    def __init__(
        self, model_id: str, guardrail_id: str, guardrail_version: str, *, client: Any = None
    ) -> None:
        super().__init__(
            model_id=model_id,
            max_tokens=MAX_TOKENS,
            guardrail_id=guardrail_id,
            guardrail_version=guardrail_version,
            guardrail_stream_processing_mode="sync",
            guardrail_latest_message=True,
            # The turn's messages are discarded after it, so nothing needs redacting, and
            # without a trace the request matches what the guardrail was tuned with.
            guardrail_trace="disabled",
            guardrail_redact_input=False,
        )
        if client is not None:
            self.client = client

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[StreamEvent, None]:
        # A worker thread reads the Bedrock stream. This signal makes it close the stream at
        # the next chunk when the turn stops early, instead of reading the answer to the end.
        stopped = threading.Event()
        kwargs["cancel_signal"] = stopped
        events = super().stream(*args, **kwargs)
        started = completed = False
        try:
            async for event in events:
                started = True
                yield event
            completed = True
        except Exception as exc:
            # A failure before the first event is a refused call (access, validation,
            # throttling); after it, the answer stream itself broke.
            if started:
                raise ModelStreamError(error_message(exc)) from exc
            raise
        finally:
            if not completed:
                stopped.set()
            await events.aclose()
