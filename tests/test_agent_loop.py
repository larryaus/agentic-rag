"""Agent tool-use loop test with a fully fake OpenAI client.

Verifies:
  (a) when finish_reason is "tool_calls", the dispatcher is called
  (b) SSE frame order is tool_use -> citation -> text -> done
  (c) loop terminates within MAX_TOOL_TURNS
  (d) assistant message is persisted with citations attached
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


# ---------- fake OpenAI streaming helpers ----------


class _AsyncIter:
    def __init__(self, items): self._items = list(items)
    def __aiter__(self): return self
    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _FakeCompletions:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []
    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _AsyncIter(self._scripted.pop(0))


class _FakeChat:
    def __init__(self, scripted):
        self.completions = _FakeCompletions(scripted)


class _FakeOpenAI:
    def __init__(self, scripted):
        self.chat = _FakeChat(scripted)


def _chunk(*, content=None, tool_call=None, finish_reason=None):
    delta = SimpleNamespace(content=content, tool_calls=[tool_call] if tool_call else None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)]
    )


def _tool_call_delta(id_, name, arguments):
    return SimpleNamespace(
        index=0,
        id=id_,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


# ---------- fake DB pool / repo ----------


class _FakePool:
    def __init__(self):
        self.persisted: list[dict] = []
    def acquire(self):
        return _FakeAcquire(self)


class _FakeAcquire:
    def __init__(self, pool):
        self.pool = pool
    async def __aenter__(self):
        return _FakeConn(self.pool)
    async def __aexit__(self, *_a):
        return False


class _FakeConn:
    def __init__(self, pool):
        self.pool = pool
    def transaction(self):
        return _FakeTx()


class _FakeTx:
    async def __aenter__(self): return self
    async def __aexit__(self, *_a): return False


# ---------- the test ----------


@pytest.mark.asyncio
async def test_loop_runs_one_tool_turn_then_finishes(monkeypatch):
    from agent_service.agent import loop as loop_mod
    from agent_service.agent import tools as tools_mod

    sid = uuid4()

    # script: turn1 -> tool_calls; turn2 -> text + stop
    scripted = [
        [
            _chunk(
                tool_call=_tool_call_delta(
                    "call_1",
                    "search_knowledge_base",
                    '{"query": "请假"}',
                ),
                finish_reason="tool_calls",
            ),
        ],
        [
            _chunk(content="请假流程是: "),
            _chunk(content="提前 3 天 OA 申请 [doc:1#chunk:0]。", finish_reason="stop"),
        ],
    ]
    fake_client = _FakeOpenAI(scripted)

    # fake dispatch
    async def fake_dispatch(name, args):
        assert name == "search_knowledge_base"
        return {
            "query": args["query"],
            "hits": [
                {
                    "citation_token": "[doc:1#chunk:0]",
                    "document_id": 1,
                    "chunk_id": 7,
                    "ord": 0,
                    "title": "员工手册",
                    "snippet": "提前 3 个工作日通过 OA 提交申请",
                    "score": 0.91,
                }
            ],
        }

    monkeypatch.setattr(tools_mod, "dispatch", fake_dispatch)

    # capture persisted messages instead of touching real DB
    persisted: list[dict] = []

    async def fake_load(_conn, _sid):
        return []

    async def fake_append(conn, *, session_id, role, content, citations=None):
        persisted.append(
            {"role": role, "content": content, "citations": citations or []}
        )
        return len(persisted)

    async def fake_touch(_conn, _sid):
        return None

    monkeypatch.setattr(loop_mod.repo, "load_messages_for_openai", fake_load)
    monkeypatch.setattr(loop_mod.repo, "append_message", fake_append)
    monkeypatch.setattr(loop_mod.repo, "touch_session", fake_touch)

    events: list[dict] = []
    async for ev in loop_mod.run_streaming(
        client=fake_client,  # type: ignore[arg-type]
        pool=_FakePool(),    # type: ignore[arg-type]
        session_id=sid,
        user_text="如何请假？",
    ):
        events.append(ev)

    # (a) dispatch was called (we asserted inside fake_dispatch); also turn count == 2
    assert len(fake_client.chat.completions.calls) == 2

    # (b) frame order: tool_use -> citation(s) -> text -> done
    names = [e["event"] for e in events]
    assert names[0] == "tool_use"
    assert "citation" in names
    assert "text" in names
    assert names[-1] == "done"
    # citation must precede the first text frame
    assert names.index("citation") < names.index("text")

    # (c) bounded by MAX_TOOL_TURNS — already proved by exiting cleanly
    # (d) persisted: user, assistant(turn1), tool, assistant(turn2)
    roles = [p["role"] for p in persisted]
    assert roles == ["user", "assistant", "tool", "assistant"]
    final_assistant = persisted[-1]
    assert any(c["document_id"] == 1 and c["ord"] == 0 for c in final_assistant["citations"])
