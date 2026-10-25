import asyncio

import pytest
from test_api import api, client  # noqa: F401

from kb_assistant.api.chat import ChatResponse


@pytest.mark.parametrize("failure", ["disconnect", "send", "cancel_twice"])
async def test_disconnect_keeps_turn_until_persisted(failure):
    first_frame = asyncio.Event()
    release = asyncio.Event()
    persisted = []
    sent = []

    async def turn(emit):
        emit({"type": "session", "sessionId": "session"})
        await release.wait()
        emit({"type": "text", "delta": "answer"})
        persisted.append("assistant")

    async def receive():
        await first_frame.wait()
        if failure == "disconnect":
            return {"type": "http.disconnect"}
        await asyncio.Future()

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body":
            first_frame.set()
            if failure == "send":
                raise OSError("closed transport")

    task = asyncio.create_task(ChatResponse(turn, 1)({}, receive, send))
    await first_frame.wait()
    await asyncio.sleep(0.01)
    if failure == "cancel_twice":
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
    assert not task.done() and not persisted
    release.set()
    if failure == "cancel_twice":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert persisted == ["assistant"]
    assert not any(b"answer" in message.get("body", b"") for message in sent)


async def test_deadline_bounds_stalled_turn():
    cancelled = asyncio.Event()

    async def turn(emit):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async def receive():
        await asyncio.Future()

    async def send(message):
        pass

    start = asyncio.get_running_loop().time()
    await ChatResponse(turn, 0.03)({}, receive, send)
    await asyncio.wait_for(cancelled.wait(), 0.1)
    assert asyncio.get_running_loop().time() - start < 0.2


async def test_model_deadline_persists_failure_before_response_returns(client, api):  # noqa: F811
    class Stalled:
        async def stream(self, *args):
            await asyncio.Future()
            yield

    api[1].model = Stalled()
    api[1].settings.chat_turn_deadline_seconds = 0.3
    response = await client.post("/", json={"message": "question"})
    assert "event: error" in response.text
    session = (await api[1].store.list_sessions("owner", 25, None))["items"][0]
    rows = (await api[1].store.list_messages(session["sessionId"], 50))["items"]
    assert len(rows) == 2 and rows[1]["stopReason"] == "error"
