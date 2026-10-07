# Enterprise Knowledge Base Assistant

A serverless, citation-grounded Q&A assistant for shared company documents, built on AWS
and deployed to a real account. Users upload documents, ask questions in natural
language, and get streamed answers whose claims link back to the source files.

**Live demo: https://d3efqlxqt2imc9.cloudfront.net**

<img width="1428" height="754" alt="image" src="https://github.com/user-attachments/assets/1f5b56c4-fdf5-4f39-b8b7-971d3858e2ff" />


Sign-in is invite-only: self-registration is disabled so that model spend stays bounded.
Ask me for a demo account and I will create one for you.

## What it does

- **Grounded answers with citations.** An agent loop on Amazon Bedrock decides when to
  search the knowledge base, then answers from the retrieved passages. Each claim carries
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

## Architecture

```text
            Browser ── React 19 + TypeScript single-page app
               │
               ├── static site ──► CloudFront ──► private S3 bucket
               │
               ├── sign-in ──────► Cognito Hosted UI (authorization code + PKCE)
               │
               ├── documents,    ► API Gateway HTTP API ──► Lambda
               │   sessions        (Cognito JWT authorizer, access-token scope)
               │
               └── chat ─────────► Lambda Function URL (RESPONSE_STREAM / SSE)
                                   verifies the access token in code
                                        │
                      ┌─────────────────┴─────────────────┐
                      ▼                                   ▼
           Bedrock ConverseStream                     DynamoDB
              agent tool loop                  private conversations,
                      │                          document records
           search_knowledge_base
                      │
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

| Area | AWS services | What it demonstrates |
|---|---|---|
| Retrieval-augmented generation | Bedrock Knowledge Bases, Titan embeddings, S3 Vectors | Chunking, embedding, vector search, grounding |
| Agentic inference | Bedrock ConverseStream with tool use | A model-driven tool loop with bounded iterations |
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
- S3 and DynamoDB use a customer-managed KMS key. Each Lambda has its own role with
  named resources and only the actions it needs; a test pins the exact action list.
- The frontend bucket is private and served only through CloudFront. Model output is
  rendered as Markdown with raw HTML left inert.
- Tokens, presigned URLs and document text are never logged.

## Testing

```bash
npm ci
python3 -m venv .venv && .venv/bin/python -m pip install -e './evals[test]'
npm run verify
```

`npm run verify` runs strict TypeScript checking, zero-warning ESLint, the Vitest suites
(backend handlers, the agent loop, React components, and CDK template assertions), a
production frontend build, an offline `cdk synth`, and the Python dataset tests. No test
needs AWS credentials.

## Continuous integration and deployment

GitHub Actions (`.github/workflows/ci.yml`) runs `npm run verify` on every pull request
and every push to `main`. After a push to `main` passes, three more jobs run in order:

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
backend/   TypeScript Lambda handlers, the Bedrock agent loop, auth and storage helpers
frontend/  React 19 + Vite single-page application
infra/     AWS CDK v2 stacks: frontend hosting, storage, knowledge base, auth, API, budget
shared/    Type-only API and SSE contracts used by both sides
evals/     Python golden-dataset schema for retrieval evaluation
samples/   Three sample documents: an employee handbook, a product FAQ, a support runbook
tools/     Local mock server and the frontend publish script
```

## Run it locally without AWS

A mock server stands in for Cognito, the API, S3 and the streaming chat endpoint, so the
UI can be exercised end to end with no account:

```bash
npm run dev:mock
```

Its settings live in `frontend/.env.mock`, which Vite loads only in mock mode, so they
cannot reach a deployed build.

## Deploy to your own account

Prerequisites: Node.js 24, the AWS CLI v2 with credentials, and Amazon Bedrock access to
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

## Cost

Approximate, for a small demo in `ap-southeast-2`:

| | |
|---|---|
| Idle | about US$1–2 a month, mostly the KMS key |
| Per question | about US$0.03–0.05 on Claude Sonnet 4.6 |
| Embedding a document | fractions of a cent |

## Roadmap

Not built yet, with the hooks already in place:

- Bedrock Guardrails and PII masking
- Department and date metadata filtering (the Cognito attribute and document metadata
  already exist)
- Hybrid retrieval and reranking
- A human-approval step for sensitive actions
- Token, latency and cost dashboards
- A regression runner over the golden dataset in `evals/`
- WAF and a custom domain
