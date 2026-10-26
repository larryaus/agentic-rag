"""A deterministic OpenAI-compatible server for tests and the local acceptance run.

Chat: the first call of a turn asks to search with the user's question; once a search
result is in the transcript it answers from the first result and cites it. Embeddings:
hashed bag-of-words vectors, so passages sharing words with a query rank near it.
Non-streaming calls answer the guardrail classifier, or the evaluation judge
(kb_evals.answers), which here supports a fact when every number in it is in the answer.

    uvicorn fake_llm_server:app --port 8080
"""

import hashlib
import json
import math
import os
import re
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

DIMENSION = int(os.environ.get("FAKE_EMBEDDING_DIMENSION", "1536"))
REFUSE = "ignore previous instructions"

app = FastAPI()
app.state.requests = []


def embed(text: str, dimension: int = DIMENSION) -> list[float]:
    vector = [0.0] * dimension
    for word in re.findall(r"\w+", text.lower()):
        digest = hashlib.sha256(word.encode()).digest()
        vector[int.from_bytes(digest[:4], "little") % dimension] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
    body = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "fake",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(body)}\n\n"


def answer_for(messages: list[dict[str, Any]]) -> tuple[list[str], str]:
    last = messages[-1]
    if last["role"] == "tool":
        payload = json.loads(last["content"]) if last["content"].startswith("{") else {}
        results = payload.get("results", [])
        if not results:
            return ["I could not find that in the knowledge base."], "stop"
        first = results[0]
        sentence = " ".join(first["text"].split())[:120]
        return [f"From {first['title']}: ", sentence, f" [ref:{first['ref']}]"], "stop"
    question = next(m["content"] for m in reversed(messages) if m["role"] == "user")
    return [json.dumps({"query": question})], "tool_calls"


def judge(prompt: str) -> list[bool]:
    answer, facts = prompt.split("\n\nAnswer:\n", 1)[1].split("\n\nFacts:\n", 1)
    facts = re.sub(r"(?m)^\d+\. ", "", facts)
    numbers = [re.findall(r"\d+(?:[.,:]\d+)*", line) for line in facts.splitlines()]
    return [bool(found) and all(number in answer for number in found) for found in numbers]


@app.post("/v1/chat/completions")
async def completions(request: Request) -> Any:
    body = await request.json()
    app.state.requests.append(body)
    messages = body["messages"]
    if not body.get("stream"):
        if messages[0]["content"].startswith("You grade answers"):
            verdict: dict[str, Any] = {"supported": judge(messages[-1]["content"])}
        else:
            verdict = {"allowed": REFUSE not in messages[-1]["content"].lower()}
        return JSONResponse(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "fake",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": json.dumps(verdict)},
                        "finish_reason": "stop",
                    }
                ],
            }
        )
    parts, finish = answer_for(messages)

    def frames() -> Any:
        if finish == "tool_calls":
            yield chunk(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"call_{len(app.state.requests)}",
                            "type": "function",
                            "function": {"name": "search_knowledge_base", "arguments": ""},
                        }
                    ],
                }
            )
            # Arguments arrive split across deltas, as real servers send them.
            arguments = parts[0]
            middle = len(arguments) // 2
            for piece in (arguments[:middle], arguments[middle:]):
                yield chunk({"tool_calls": [{"index": 0, "function": {"arguments": piece}}]})
        else:
            for part in parts:
                yield chunk({"content": part})
        yield chunk({}, finish)
        if (body.get("stream_options") or {}).get("include_usage"):
            usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
            yield "data: " + json.dumps(
                {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 0,
                 "model": "fake", "choices": [], "usage": usage}
            ) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(frames(), media_type="text/event-stream")


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> Any:
    body = await request.json()
    inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
    return {
        "object": "list",
        "model": body["model"],
        "data": [
            {"object": "embedding", "index": index, "embedding": embed(text)}
            for index, text in enumerate(inputs)
        ],
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }


@app.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}
