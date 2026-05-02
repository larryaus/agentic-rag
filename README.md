# 智答 · Agentic AI 知识助手（RAG）

一个**最小可运行**的端到端 Agentic AI 系统：用户上传文档 → 切块 / 向量化 / 入库 → 用户提问时
Claude 通过**工具调用循环**自主决定何时检索、检索什么、是否再次检索、何时收尾，最终以 SSE
流式吐字 + 可追溯的引用（`[doc:#chunk:]`）回应。

---

## 一、系统概述

| 维度 | 选型 | 说明 |
|---|---|---|
| 编排 | Anthropic Claude（`claude-sonnet-4-6` 主循环 / `claude-haiku-4-5-20251001` 查询改写） | tool-use 循环；prompt caching |
| 检索 | pgvector（HNSW）+ tsvector(BM25-ish)+ RRF + Voyage `rerank-2` | 单库混合检索 |
| 嵌入 | Voyage `voyage-3`（1024 维，多语言含中文） | 文档/查询双 input_type |
| 后端 | Python 3.11 + FastAPI + asyncpg + sse-starlette | 全异步，SSE 流式 |
| 存储 | PostgreSQL 16 + pgvector 扩展 | 单一存储后端 |
| 前端 | vanilla HTML / JS + Tailwind CDN | 无 Node 构建链 |
| 部署 | docker-compose（postgres + redis + agent） | `docker compose up` 即跑 |

---

## 二、架构图

```
                ┌─────────────────────────────┐
                │ 浏览器（vanilla SPA）        │
                │  /index.html  /app.js       │
                └──────────────┬──────────────┘
                               │ fetch + SSE
                               ▼
┌──────────────────────────────────────────────────────────┐
│ FastAPI agent-service  :8000                              │
│                                                            │
│  /api/v1/ingest ─────► ingest.pipeline ──► chunker        │
│                                          ─► Voyage embed  │
│                                          ─► pgvector      │
│                                                            │
│  /api/v1/chat   ─────► agent.loop (tool-use)              │
│                          ├─ search_knowledge_base ─┐      │
│                          ├─ fetch_document         │      │
│                          └─ list_documents         ▼      │
│                                  retrieval.hybrid (RRF)   │
│                                       │                    │
│                                       ▼                    │
│                                 Voyage rerank-2            │
│                                                            │
│  /api/v1/documents  /api/v1/sessions/{id}  /healthz       │
└──────────────────────────┬───────────────────────────────┘
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
      ┌───────────────┐         ┌──────────────┐
      │ Postgres 16   │         │ Redis 7      │
      │  + pgvector   │         │ (预留)        │
      │  + pg_trgm    │         └──────────────┘
      └───────────────┘
```

---

## 三、模块说明

| 路径 | 职责 |
|---|---|
| `src/agent_service/main.py` | FastAPI app、lifespan、静态前端挂载 |
| `src/agent_service/config.py` | pydantic-settings，读取 `.env` |
| `src/agent_service/db/pool.py` | asyncpg 连接池 + pgvector codec 注册 |
| `src/agent_service/db/repo.py` | documents / chunks / sessions / messages CRUD |
| `src/agent_service/ingest/loaders.py` | MIME 分发：PDF / MD / HTML / TXT → 文本 |
| `src/agent_service/ingest/chunker.py` | 递归 token 切块（hand-rolled，支持 tiktoken 与 char 兜底） |
| `src/agent_service/ingest/pipeline.py` | load → chunk → embed → persist 流水线 |
| `src/agent_service/retrieval/embeddings.py` | Voyage 异步 batch embedding |
| `src/agent_service/retrieval/hybrid.py` | 向量 + tsvector + RRF 单条 SQL |
| `src/agent_service/retrieval/reranker.py` | Voyage `rerank-2`（开关） |
| `src/agent_service/retrieval/query_rewrite.py` | Haiku 改写检索查询 |
| `src/agent_service/agent/prompts.py` | 中文 system prompt |
| `src/agent_service/agent/tools.py` | 3 个工具的 schema + dispatch |
| `src/agent_service/agent/loop.py` | 流式 tool-use 循环（核心） |
| `src/agent_service/agent/citations.py` | 引用 token 解析 |
| `src/agent_service/api/*.py` | FastAPI 路由 |
| `src/agent_service/sse.py` | 简易 SSE 事件通道 |
| `web/` | 前端单页应用 |
| `migrations/` | 原始 SQL 迁移（`psql -f`） |
| `samples/` | 示例知识库文档（中文） |
| `scripts/seed.py` | 启动后幂等导入 samples |
| `tests/` | 单测 |

---

## 四、API

全部前缀 `/api/v1`（`/healthz` 除外）。

| 方法 | 路径 | 入参 | 返回 |
|---|---|---|---|
| `POST` | `/api/v1/ingest` | multipart：`file`、可选 `title` | `{document_id, title, chunk_count, sha256, duplicated}` |
| `POST` | `/api/v1/chat` | JSON：`{session_id?, message, document_ids?, rerank?}` | `text/event-stream` |
| `GET` | `/api/v1/documents` | `limit`、`offset`、`q` | `[DocumentSummary]` |
| `GET` | `/api/v1/sessions/{id}` | — | `SessionDetail`（含 messages + citations） |
| `GET` | `/healthz` | — | `{status, db}` |

### SSE 事件清单（`/api/v1/chat`）

| event | data | 说明 |
|---|---|---|
| `session` | `{session_id}` | 首帧；带本次会话 ID |
| `tool_use` | `{name, input}` | 模型调用某个工具 |
| `citation` | `{document_id, chunk_id, ord, title, snippet, score}` | 工具命中条目（用于前端 chip） |
| `text` | `{delta}` | 模型流式吐字 |
| `done` | `{session_id, stop_reason}` | 终止 |
| `error` | `{message}` | 异常 |

---

## 五、数据流（序列图）

### 5.1 入库

```
User ─upload─► /ingest
  └► loaders.detect_mime + extract_text
  └► chunker.chunk_text  (~512 token, 64 overlap)
  └► embeddings.embed_documents (Voyage voyage-3, batch=64)
  └► repo.insert_document + insert_chunks_bulk (一个事务)
  ◄── {document_id, chunk_count, sha256}
```

### 5.2 提问（关键链路）

```
User ─POST /chat─► chat.router
   └► repo.create_session(若 session_id 为空)
   └► sse.send("session", {sid})
   └► loop.run_streaming():
        load_messages_for_anthropic
        loop:
          messages.stream(model=sonnet-4-6, system=cached, tools=cached, history)
            ──► sse.send("text", delta) for each text delta
          final = stream.get_final_message()
          if stop_reason != "tool_use": break
          for tu in final.content where type==tool_use:
            sse.send("tool_use", ...)
            result = tools.dispatch(tu.name, tu.input)
              search_knowledge_base ──►
                 query_rewrite (Haiku)
                 embed_query (Voyage)
                 hybrid_search (vector+BM25+RRF)
                 rerank (Voyage)
                 hits with citation_token
            for hit: sse.send("citation", hit)
            append tool_result to history
        persist new messages + citations (事务)
        sse.send("done", ...)
```

---

## 六、部署运行

```bash
cp .env.example .env
# 在 .env 中填入:
#   ANTHROPIC_API_KEY=sk-ant-...
#   VOYAGE_API_KEY=pa-...

docker compose up --build
# 等到 agent 容器日志出现 "Application startup complete."
# 此时 seed.py 已经将 samples/*.md 入库

open http://localhost:8000/        # 浏览器打开前端
```

### 端到端 smoke test

```bash
# 列出文档
curl http://localhost:8000/api/v1/documents

# 上传一篇文档
curl -F "file=@samples/company_handbook.md" \
     http://localhost:8000/api/v1/ingest

# 流式聊天
curl -N -H "Content-Type: application/json" \
     -d '{"message":"请假流程是什么？"}' \
     http://localhost:8000/api/v1/chat

# 期望帧序: session -> tool_use(search_knowledge_base) -> citation*N
#          -> text(包含 [doc:1#chunk:K]) -> done

# 健康
curl http://localhost:8000/healthz
```

### 单测

```bash
pip install -e ".[dev]"
pytest -q
# 期望: 7 passed, 1 skipped
# 跳过的是需要真实 pgvector 的集成测试，设置 PGVECTOR_TEST_URL 后启用
```

---

## 七、关键设计决策

1. **混合检索 + RRF（`k=60`）**：向量 cosine 召回 40 + tsvector BM25 召回 40，按 `1/(60+rank)` 累加排名融合，避免单一通道翻车。Voyage `rerank-2` 把候选 20 → 8，提升精确率。
2. **切块**：递归 token 切块，分隔符优先级 `["\n## ", "\n### ", "\n\n", "\n", "。", "！", "？", " "]`，512 token / 64 overlap。`tiktoken` 不可用时自动退化为 ASCII/4 + 非 ASCII/1 的字符级近似。
3. **Prompt caching**：system prompt 与 tools 数组的最后一项打 `cache_control: ephemeral`，缓存命中后 system+tools 部分按 1/10 计费。
4. **双模型**：主循环用 `claude-sonnet-4-6`（最强工具调用），查询改写用 `claude-haiku-4-5-20251001`（极廉价）。
5. **引用格式**：`[doc:{document_id}#chunk:{ord}]`。工具结果中每条 hit 已经预先打上该 token，模型只需原样贴回；前端正则替换为可点击 chip。
6. **持久化**：每轮对话保存 user / assistant / user(tool_results) / assistant 四种角色到 `messages` 表（Anthropic 原生 content blocks，便于回放）；assistant 行附带 `citations` JSON。
7. **去重**：上传文件以 SHA-256 为唯一键；重复上传直接返回原 `document_id`。
8. **错误处理**：工具调用异常以 `{"error": ...}` 作为 tool_result 返回给模型，让模型自我恢复或换关键词重试，而不是中断流。

---

## 八、本地开发（不用 Docker）

```bash
# 你需要本地有 Postgres 16 + pgvector，或：
docker run -d --name pg -p 5432:5432 \
    -e POSTGRES_DB=agent_db -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres \
    pgvector/pgvector:pg16

# 跑迁移
for f in migrations/*.sql; do
  PGPASSWORD=postgres psql -h localhost -U postgres -d agent_db -f "$f"
done

# 起服务
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/agent_db
export ANTHROPIC_API_KEY=sk-ant-...
export VOYAGE_API_KEY=pa-...
pip install -e ".[dev]"
python scripts/seed.py
uvicorn agent_service.main:app --reload
```

---

## 九、后续演进（已预留扩展点）

- **鉴权**：在 `main.py` 给 `APIRouter` 挂 `Depends(verify_token)` 即可。
- **多租户**：在 `documents / chunks / sessions` 加 `tenant_id`，所有查询加 `WHERE tenant_id = $N`。
- **异步入库**：`/ingest` 仅入队（Redis Streams / Kafka），新增 worker 消费并跑 pipeline。
- **`web_search` 工具**：在 `agent/tools.py` 新增 schema + dispatch，对接 Brave / Bing。
- **中文 BM25**：当前使用 `simple` 配置（unicode 单字 + 空白）；接入 `zhparser` 或 jieba 预切可大幅提升中文关键词召回。
- **评测集**：新增 `evals/` + JSONL（question, expected_doc_id），pytest 跑 recall@k。
- **观测**：接入 OpenTelemetry，把 token 用量、缓存命中率、工具调用计数打成指标。

---

## 十、目录结构

```
.
├── README.md
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── entrypoint.sh
├── .env.example
├── migrations/
│   ├── 001_extensions.sql
│   ├── 002_documents.sql
│   └── 003_sessions.sql
├── samples/
│   ├── company_handbook.md
│   └── product_faq.md
├── scripts/seed.py
├── web/{index.html, app.js, style.css}
├── src/agent_service/
│   ├── main.py  config.py  deps.py  schemas.py  sse.py
│   ├── api/{health, ingest, chat, documents, sessions}.py
│   ├── db/{pool, repo}.py
│   ├── ingest/{loaders, chunker, pipeline}.py
│   ├── retrieval/{embeddings, hybrid, reranker, query_rewrite}.py
│   └── agent/{prompts, tools, loop, citations}.py
└── tests/{conftest, test_chunker, test_retriever, test_agent_loop}.py
```
