# Enterprise Knowledge Base Assistant

A citation-grounded Q&A assistant for shared company documents. Users upload
documents, ask questions in natural language, and get streamed answers whose claims
link back to the source files.

One Python FastAPI backend runs in two deployments:

- **Local, on any machine:** `docker compose up` with Postgres and pgvector, Keycloak
  for sign-in, and any OpenAI-compatible model: a hosted API with a key, or Ollama.
  No AWS account needed. See [Run it locally](#run-it-locally).
- **AWS, serverless:** Lambda, Bedrock, DynamoDB, S3 and Cognito, deployed with CDK.
  This is the live demo below.

**Live demo: https://d3efqlxqt2imc9.cloudfront.net**

<img width="1428" height="754" alt="image" src="https://github.com/user-attachments/assets/1f5b56c4-fdf5-4f39-b8b7-971d3858e2ff" />


Sign-in is invite-only: self-registration is disabled so that model spend stays bounded.
Ask me for a demo account and I will create one for you.

## What it does

- **Grounded answers with citations.** An agent loop built on
  [Strands Agents](https://strandsagents.com) decides when to search the knowledge base,
  then answers from the retrieved passages. Each claim carries
  a numbered chip that downloads the source document.
- **Streaming chat.** Answers arrive token by token over Server-Sent Events, rendered as
  Markdown.
- **Self-service document library.** Upload PDF, Markdown, text or HTML from the browser.
  Each document moves through `UPLOADING → INGESTING → READY` (or `FAILED`), and failed
  uploads can be cleared from the list.
- **Private conversations, shared documents.** Chat history is per user; the document
  library is shared across the organisation.
- **Runs for about a dollar a month when idle.** Every component is serverless or
  on-demand, including the vector store.

## Run it locally

Prerequisites: Docker with Compose v2, and an API key for an OpenAI-compatible model
(or [Ollama](#use-ollama-instead-of-a-hosted-model)).

```bash
cp .env.example .env        # then set LLM_API_KEY in .env
docker compose up --build
```

Open http://localhost:8000 and sign in as `demo` / `demo-password`. Upload the files in
`samples/` from the Documents panel, wait for them to show `READY`, and ask a question
such as "How many days of annual leave do employees get?".

Compose starts four services: the app on port 8000 (it also serves the frontend),
Postgres with pgvector, Keycloak on port 8080 with a seeded `kb` realm, and, with the
`ollama` profile, a local model server. Ports are bound to `127.0.0.1` only, because
Keycloak runs in development mode with an `admin` / `admin` console. Documents live in a Docker volume, and
`docker compose down -v` removes them along with the database.

To try the whole stack without any model key, use the deterministic fake model the
tests use:

```bash
docker compose -f docker-compose.yml -f docker-compose.fake-llm.yml up --build
```

### Use Ollama instead of a hosted model

```bash
# In .env: the commented Ollama block from .env.example
docker compose --profile ollama up -d --build
docker compose exec ollama ollama pull qwen3:8b
docker compose exec ollama ollama pull nomic-embed-text
```

The model must support tool calling. `OLLAMA_CONTEXT_LENGTH` is set to 32768 in
`docker-compose.yml`, and `LLM_MAX_CONTEXT_CHARS` keeps each request inside it by
dropping the oldest conversation history first. The question check gives the model 512
tokens and 20 seconds; a reasoning model that thinks for longer fails that check open,
so with such a model you may prefer `GUARDRAIL_INPUT_CHECK=off`.

### Switching embedding models

The vector index is tied to the embedding model that built it, and the app refuses to
start against a different one. To switch, rebuild the index with the app stopped:

```bash
docker compose stop app
docker compose run --rm app python -m kb_assistant.adapters.local.reindex
docker compose up -d app
```

### Frontend only

A mock server stands in for the sign-in provider, the API and the streaming chat
endpoint, for UI work with no backend at all:

```bash
npm run dev:mock
```

Its settings live in `frontend/.env.mock`, which Vite loads only in mock mode, so they
cannot reach a deployed build.

## Two deployments, one core

```text
                    React frontend (generic OpenID Connect sign-in)
                                  │
                  FastAPI app: routes, SSE chat, citations, history,
                  upload rules, paging, contracts
                                  │
                  Strands Agents loop: search tool, citation numbering,
                  search-step limit, context budget, answer filter
                                  │
           ┌──────────────────────┴───────────────────────┐
           ▼                                              ▼
    adapters/local                                  adapters/aws
    Strands model: OpenAI-compatible chat           Strands model: Bedrock ConverseStream
    OpenAI-compatible embeddings                    Titan Text Embeddings v2
    Postgres: records + hybrid pgvector search      Bedrock Knowledge Base + S3 Vectors
    files on a volume, signed URLs                  S3, presigned URLs
    in-process ingestion worker                     EventBridge ingest + reconciler
    model classifier + pattern masking              Bedrock Guardrails
    Keycloak                                        Cognito
```

| | Local | AWS |
|---|---|---|
| Runs as | uvicorn in Docker Compose | Lambda (Lambda Web Adapter for HTTP), CDK |
| Model | any OpenAI-compatible API, or Ollama | Bedrock (Claude) |
| Search | Postgres + pgvector, vector and keyword fused with RRF | Bedrock Knowledge Base, S3 Vectors |
| Records | Postgres | DynamoDB single table |
| Files | Docker volume, HMAC-signed URLs | S3 (KMS), presigned URLs |
| Sign-in | Keycloak | Cognito |
| Ingestion | in-process worker with leases | per-document ingestion + reconciler |
| Safety | model classifier for questions, pattern masking for answers | Bedrock Guardrails |

The routes, agent loop, citations, history rules and API contract are shared, so a fix
to any of them lands in both deployments. The agent loop runs on Strands Agents: the
search tool, citation numbering, the search-step limit, the context budget and answer
masking are hooks and middleware around it, and each deployment's chat adapter is a
Strands model provider. `container.py` is the only place that picks an adapter, and it
imports adapters lazily: the Lambda package never imports the local dependencies, and
the Docker image never creates an AWS client (boto3 is installed there only because
Strands depends on it). Tests enforce both.

## AWS architecture

```text
            Browser ── React 19 + TypeScript single-page app
               │
               ├── static site ──► CloudFront ──► private S3 bucket
               │
               ├── sign-in ──────► Cognito Hosted UI (authorization code + PKCE)
               │
               ├── documents,    ► API Gateway HTTP API ──► Lambda
               │   sessions        (Cognito JWT authorizer, access-token scope)
               │                   also verifies the access token in code
               │
               └── chat ─────────► Lambda Function URL (RESPONSE_STREAM / SSE)
                                   verifies the access token in code
                                        │
                      ┌─────────────────┴─────────────────┐
                      ▼                                   ▼
             Strands Agents loop                      DynamoDB
          ┌───────────┴───────────┐            private conversations,
          ▼ model calls           ▼ tool calls   document records
Bedrock ConverseStream  search_knowledge_base
+ Guardrail (sync mode)           │
                      ┌───────────┘
                      ▼
           Bedrock Knowledge Base ── Titan Text Embeddings v2
                      │
        ┌─────────────┴──────────────┐
        ▼                            ▼
 S3 documents bucket           S3 Vectors index
 (KMS, versioned)              (1024-dim, cosine)
        │
        └─ EventBridge "Object Created" ─► ingest Lambda ─► per-document ingestion

 EventBridge rate(1 minute) ─► reconciler Lambda
                               ├─ polls ingestion status until terminal
                               └─ expires abandoned uploads
```

The HTTP Lambdas run the Python FastAPI app through the Lambda Web Adapter. Each
function mounts only its own routes and initializes their AWS clients before serving
requests. Ingestion and reconciliation use direct Python Lambda handlers.

| Area | AWS services | What it demonstrates |
|---|---|---|
| Retrieval-augmented generation | Bedrock Knowledge Bases, Titan embeddings, S3 Vectors | Chunking, embedding, vector search, grounding |
| Agentic inference | Strands Agents on Bedrock ConverseStream with tool use | A model-driven tool loop with bounded iterations |
| Safety | Bedrock Guardrails | Content and prompt-attack filtering, masking of sensitive data in answers |
| Identity | Cognito user pool, resource-server scope | OAuth 2.0 code flow with PKCE, access-token-only APIs |
| Data protection | KMS customer-managed key, private S3, scoped IAM | Encryption at rest, least privilege per Lambda |
| State | DynamoDB single-table design | Per-user ownership checks, TTL, pagination |
| Operations | CloudWatch Logs and alarms, X-Ray, AWS Budgets | Tracing, error alarms, a spend alert |
| Delivery | AWS CDK v2 (six stacks), CloudFront, GitHub Actions | Infrastructure as code, CI and approved deploys with keyless access through OIDC |

## Design decisions

- **Two entry points for the API, on purpose.** API Gateway HTTP API gives a managed
  Cognito authorizer for the ordinary JSON endpoints but cannot stream. Chat therefore
  uses a Lambda Function URL in `RESPONSE_STREAM` mode and verifies the token itself
  before opening the stream.
- **S3 Vectors instead of an always-on vector database.** A vector store that bills by
  usage keeps the idle cost near zero, which matters for a system that sits unused most
  of the day.
- **Per-document ingestion with a reconciler.** Each upload is ingested directly rather
  than by re-syncing the whole data source. A one-minute reconciler polls accepted
  ingestion and checks S3 for completed uploads whose event or ingestion request failed.
  After the ten-minute recovery interval it retries with the same idempotency token,
  up to three scheduled attempts. Exhausted uploads become `FAILED` with their files
  retained; only a confirmed missing S3 object is treated as an abandoned upload.
- **Retrieval is a tool, and citations are resolved server-side.** The agent decides
  when to search, and citation chips are built from the chunks that were actually
  returned, so a chip cannot point at a document the model invented.
- **Model IDs are configuration.** Chat and embedding models are CDK context values, and
  the IAM grant is derived from them, so switching model or region is a one-line change.
- **One app, two deployments.** The same FastAPI app serves both, with ports for the
  model, search, records, files, ingestion, guardrail and sign-in. The local store keeps
  DynamoDB's item and cursor shapes, so the shared routes cannot tell the two apart.
- **Local ingestion is safe to run twice.** Each claim on a document takes a lease and a
  new generation, and every later write checks both the generation and the embedding
  model, so a reclaimed lease or a worker left on an old model can never overwrite newer
  work. Chunks are embedded before one transaction replaces them.

## What the first real deployment surfaced

The test suite mocks every AWS call, and all of it was green before the first deploy.
Three things still only showed up against the real services:

1. **A listed model is not an invocable model.** The newest Claude profiles appear in
   the region's catalogue but were not enabled for the account, and Anthropic models
   also need a one-time use-case form and a Marketplace subscription that completes a
   few minutes after the first call. The fix was to probe each profile with a real
   request and default to one that answers.
2. **Direct ingestion needs a second permission.** `IngestKnowledgeBaseDocuments` is
   also authorised against `bedrock:StartIngestionJob`. A unit test had asserted that
   this action was never granted, so the test was enforcing the bug.
3. **S3 Vectors caps filterable metadata at 2 KB per vector.** Bedrock stores each
   chunk's text as metadata, so normal-sized chunks were rejected and documents ended
   `FAILED` with no reason given. Ingesting a tiny document isolated the cause; the fix
   marks Bedrock's text fields as non-filterable when the index is created.

Each fix is covered by an infrastructure test so it cannot quietly regress.

## Security model

- API Gateway accepts only Cognito **access** tokens carrying the `kb-api/access` scope;
  ID tokens do not carry it and are rejected.
- Conversations are private. Every read loads the session record and checks its owner
  before any message is queried.
- Documents are organisation-shared by design: any signed-in user can list, search and
  download them. The uploader is recorded for audit, not authorisation.
- The chat Function URL is publicly reachable because browsers cannot SigV4-sign
  requests. It rejects unauthenticated calls before any model call, and reserved
  concurrency caps how many can run at once. CORS is treated as a browser convention,
  not access control.
- A Bedrock guardrail checks every question for harmful content and prompt attacks,
  and refuses the turn before the model sees it. It checks every answer too, and
  replaces card numbers, bank details, passwords, access keys and government IDs with a
  placeholder such as `{CREDIT_DEBIT_CARD_NUMBER}` before the text is streamed or
  stored. Contact details are left alone, because documents legitimately hold them.
- The guardrail's limits are deliberate. It checks the new question and the answer, not
  the retrieved document text, so instructions planted in a document are not caught on
  the way in. A refused turn is left out of later context, so it cannot be replayed.
  Sensitive data a user types is stored as typed, and documents are not scrubbed when
  they are ingested.
- S3 and DynamoDB use a customer-managed KMS key. Each Lambda has its own role with
  named resources and only the actions it needs; a test pins the exact action list.
- The frontend bucket is private and served only through CloudFront. Model output is
  rendered as Markdown with raw HTML left inert.
- Tokens, presigned URLs and document text are never logged.

The local deployment's safety layer is weaker than Bedrock Guardrails, and says so:

- Questions are screened by one extra call to the same model, acting as a classifier.
  If that call fails or returns something unreadable, the question is allowed (fail
  open), so treat it as a best-effort filter. `GUARDRAIL_INPUT_CHECK=off` removes it
  and its extra model call.
- Answers are masked by patterns for card numbers, IBANs, AWS keys, labelled passwords,
  and US, UK and Canadian identifiers, with the same placeholders Bedrock uses. Every
  pattern matches at most 86 characters, so the stream holds back the last 87 and never
  releases a cut through a match, so the streamed text is what masking the whole answer
  would give. (A pathologically long chain of overlapping matches is cut by force and
  can repeat a placeholder; it never releases the raw text.) If the answer did not
  finish normally (the stream failed, or it hit the token limit, a content filter, the
  search-step limit or a stop reason the server does not document), the held-back tail
  is dropped rather than released.
- Pattern masking has gaps. Anything the patterns do not know passes through. A labelled
  password is masked only up to its first 64 characters. A card number in the same run
  of digits as other numbers (an expiry such as `12/26` or a CVV right after it, or two
  card numbers in a row) is not recognised, and nor is an IBAN followed directly by more
  capitals or digits.
- Signed file URLs are HMAC tokens bound to one key, one operation and one content
  type, expiring after five minutes. The signing secret is generated on first start and
  kept with the files.

## Testing

```bash
npm ci
python3.12 -m venv .venv
.venv/bin/python -m pip install -c backend/requirements-aws.lock -e './backend[aws,local,dev]' -e './evals[test]'
npm run verify
npm run verify:artifact  # requires Docker

# Postgres-backed tests for the local deployment (skipped without TEST_DATABASE_URL):
docker run -d --name kb-test-pg -p 5433:5432 -e POSTGRES_PASSWORD=postgres pgvector/pgvector:0.8.7-pg17
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/postgres .venv/bin/pytest -q backend/tests
```

`npm run verify` runs TypeScript checking, ESLint, frontend and CDK Vitest tests,
contract fixtures, the frontend build, CDK synthesis, Ruff, strict mypy, Python backend
pytest, and the evaluation dataset tests. Bundling downloads pinned Python packages;
no test calls AWS or a model API. `npm run verify:artifact` boots the ARM64 package in
the Lambda Python 3.12 image with networking disabled. The adapter layer, streaming
transport, and IAM still require validation in AWS. The same constraints pin the
shared runtime packages in development, CI, and the Lambda asset. The artifact gate
also constructs every AWS adapter, checks fail-closed JWT verification, and exercises
incremental SSE and assistant persistence with fake adapters outside the shipped asset.

## Evaluation

`evals/datasets/golden.json` holds 24 questions about the sample documents: their
expected facts, the documents that answer them, and verbatim evidence passages, plus
questions the documents do not cover. A test fails if editing a sample document
invalidates any evidence.

Two evaluations run against the real local deployment: the samples are uploaded through
the API and ingested by the worker, so they cover the shipped extraction, chunking,
embedding and hybrid search.

- **Retrieval, on every pull request.** The Postgres tests score where each question's
  evidence ranks in the retrieved chunks (hit@1, recall@k, mean reciprocal rank) and
  fail if any metric falls below `evals/baselines/retrieval-pipeline.json`. Embeddings
  come from the fake model server, so the scores are deterministic and the gate catches
  pipeline regressions (dropping keyword search, for example, fails it), not the quality
  of a real embedding model. After an intended change, rerun with
  `UPDATE_EVAL_BASELINE=1` and commit the new baseline.
- **Retrieval and answers with real models, weekly and on demand.** The `Evals`
  workflow repeats the retrieval scoring with a real embedding model, then asks every
  question and checks each answer: whether it cites an expected document, whether a
  model judge finds each expected fact, and whether uncovered questions are declined
  without citations. It runs only when the `EVAL_LLM_API_KEY` secret is set; the
  `EVAL_LLM_BASE_URL`, `EVAL_LLM_MODEL`, `EVAL_EMBEDDING_MODEL`,
  `EVAL_EMBEDDING_DIMENSION` and `EVAL_JUDGE_MODEL` variables point it at any
  OpenAI-compatible endpoint. Reports go to the run summary; there is no answer
  baseline yet, so it reports rather than fails.

To run them yourself against a pgvector server, export the model settings the app reads
(`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSION`;
`JUDGE_MODEL` optionally picks a different judge), then:

```bash
.venv/bin/python -m kb_evals retrieval --database-url postgresql://postgres:postgres@localhost:5433/postgres
.venv/bin/python -m kb_evals answers --database-url postgresql://postgres:postgres@localhost:5433/postgres
```

Each run uses a fresh database on that server and drops it afterwards. The sample set is
small (eight chunks), so these numbers move in large steps; a larger corpus would make
them more sensitive.

## Continuous integration and deployment

GitHub Actions (`.github/workflows/ci.yml`) runs `npm run verify` and
`npm run verify:artifact` on every pull request and every push to `main`. After a push
to `main` passes, three more jobs run in order:

1. **Plan** runs `cdk diff` against the deployed stacks and writes the result to the run
   summary.
2. **Deploy** runs `cdk deploy --all`, but only when the plan found changes, and only
   after a reviewer approves the run in the `production` GitHub environment.
3. **Publish frontend** rebuilds the frontend against the deployed stacks' outputs and
   publishes it to CloudFront. It waits for the deploy, so the frontend never goes out
   ahead of its backend, and it does not run if the deploy fails or is rejected.

A push that changes no infrastructure skips the deploy and its approval and publishes
straight away.

No job holds AWS keys. Each exchanges GitHub's OIDC token for a short-lived IAM role:

| Role | Trusted for | Can do |
|---|---|---|
| Plan | runs on `main` | read the deployed stacks, through the CDK lookup role |
| Deploy | runs approved into the `production` environment | deploy, through the CDK bootstrap roles |
| Publish | runs on `main` | write to the site bucket, invalidate the distribution, read the stack outputs |

The deploy role is as powerful as a local `cdk deploy`, which is why it trusts the
environment rather than the branch: GitHub issues that token only after the approval. A
run whose commit is no longer the tip of `main` refuses to deploy, so approving an old
run cannot roll the stacks back.

The budget stack needs its alert address at deploy time, so CI leaves it alone; change it
with a local `cdk deploy -c budgetAlertEmail=...`.

## Repository layout

```text
backend/   Python FastAPI app, shared agent core, AWS adapters and Lambda entry points
frontend/  React 19 + Vite single-page application
infra/     AWS CDK v2 stacks: frontend hosting, storage, knowledge base, auth, API, budget
shared/    Typed API/SSE contracts and JSON compatibility fixtures
docker/    The local deployment's image and Keycloak realm
evals/     Golden dataset, retrieval and answer scoring, and the evaluation harness
samples/   Three sample documents: an employee handbook, a product FAQ, a support runbook
tools/     Frontend mock/publish scripts, contract fixture generator and ARM64 artifact checks
```

## Deploy to your own account

Prerequisites: Node.js 24, Python 3.12, the AWS CLI v2 with credentials, and Amazon Bedrock access to
the chat model in the target region. A profile being listed does not mean the account
may invoke it, so confirm the model answers in the Bedrock console playground first. The
default, `au.anthropic.claude-sonnet-4-6`, is the one verified for Sydney
(`ap-southeast-2`); override it with `-c chatModelId=...`.

```bash
npx -w infra cdk bootstrap
npx -w infra cdk deploy --all --outputs-file cdk-outputs.json \
  -c budgetAlertEmail=you@example.com

aws cognito-idp admin-create-user \
  --user-pool-id <UserPoolId> \
  --username <email> \
  --user-attributes Name=email,Value=<email> Name=email_verified,Value=true

npm run frontend:publish    # builds the UI and publishes it to CloudFront
```

To let CI deploy and publish from your own fork, set `githubRepository` in
`infra/cdk.json` to `owner/name` before deploying, then:

- add four repository variables: `AWS_REGION`, and `AWS_PLAN_ROLE_ARN`,
  `AWS_DEPLOY_ROLE_ARN` and `AWS_PUBLISH_ROLE_ARN` from the stack outputs of the same
  names;
- create a GitHub environment named `production`, add yourself as a required reviewer,
  and limit its deployment branches to `main`.

Setting only `AWS_PUBLISH_ROLE_ARN` publishes the frontend without deploying
infrastructure. Leave `githubRepository` empty to create no CI access at all.

`budgetAlertEmail` is optional. When set, it creates an AWS Budget that emails at 80% and
100% of `monthlyBudgetUsd` (default 10) and when the forecast passes the limit. It is an
alert on whole-account spend, not a cap.

The deployment allows two frontend origins: the CloudFront site it creates and the local
dev origin (`http://localhost:5173`). To run the UI locally against the deployed backend:

```bash
npm run frontend:env        # writes frontend/.env from infra/cdk-outputs.json
npm run -w frontend dev
```

Remove everything with `npx -w infra cdk destroy --all`. The stacks use `DESTROY`
removal policies for convenience; a production system should retain its data.

## Rolling the Python backend out to AWS

The Lambdas changed from Node.js to Python in place: same functions, roles, routes,
table and data. The first deploy of that change goes through the usual approved CI run.
Before calling it done, check against the live stack:

1. Sign in, then open and continue a conversation created by the previous backend.
2. Ask a new question and confirm the answer streams with citation chips.
3. Upload a document and wait for `READY`, then download it from a chip.
4. Remove a `FAILED` document, if there is one.
5. Ask something the guardrail should refuse, and confirm the refusal.
6. Check the CloudWatch logs and alarms for the six functions.

To roll back, revert the merge commit on `main` and approve the redeploy; CloudFormation
restores the previous functions, and the stored data needs no migration either way.

## Cost

Approximate, for a small demo in `ap-southeast-2`:

| | |
|---|---|
| Idle | about US$1–2 a month, mostly the KMS key |
| Per question | about US$0.03–0.05 on Claude Sonnet 4.6 |
| Guardrail checks | about US$0.001 a question on top; no idle cost |
| Embedding a document | fractions of a cent |

## Roadmap

Not built yet, with the hooks already in place:

- Department and date metadata filtering (the Cognito attribute and document metadata
  already exist)
- Hybrid retrieval and reranking on AWS (the local deployment already fuses vector and
  keyword search)
- A human-approval step for sensitive actions
- Token, latency and cost dashboards
- WAF and a custom domain
