# Enterprise Knowledge Base AI Assistant

An AWS-native, citation-grounded assistant for shared company documents. This Stage 1
implementation replaces the original OpenAI/Voyage/pgvector service with CDK
infrastructure, TypeScript Lambda functions, a React 19 application, Amazon Bedrock
Knowledge Bases over S3 Vectors, Cognito authentication, DynamoDB conversation history,
and SSE-streamed answers.

The repository is designed as an AWS AI certification study project: every major feature
maps to an exam domain, infrastructure remains readable, and idle services are
serverless/on-demand.

| Feature | AWS AI exam domain |
|---|---|
| Bedrock Knowledge Base, Titan embeddings, S3 Vectors | Retrieval-augmented generation and foundation-model integration |
| ConverseStream tool loop | Agentic workflows, prompt design, and model inference |
| Cognito authorization code + PKCE | Identity, authentication, and application security |
| KMS, scoped IAM, private S3 | Responsible and secure AI solutions |
| DynamoDB history and per-user ownership | Data engineering and tenancy boundaries |
| CloudWatch logs, X-Ray, and alarms | Monitoring and operational readiness |
| CDK stacks and offline synth | Infrastructure as code and deployment architecture |

## Architecture

```text
┌──────────────────────── React 19 + TypeScript ────────────────────────┐
│                                                                      │
│  Cognito Hosted UI ── authorization code + PKCE/state/nonce ─┐      │
│                                                              ▼      │
│  Documents/sessions ─ access token ─► API Gateway HTTP API ─► Lambda│
│                                                                      │
│  Chat ─────────────── access token ─► Lambda Function URL            │
│                                     (RESPONSE_STREAM / SSE)          │
└──────────────────────────────────────────┬───────────────────────────┘
                                           │
                         ┌─────────────────┴─────────────────┐
                         ▼                                   ▼
              Bedrock ConverseStream                  DynamoDB
                 agent tool loop               private conversations
                         │
              search_knowledge_base
                         │
                         ▼
              Bedrock Knowledge Base
                         │
        ┌────────────────┴─────────────────┐
        ▼                                  ▼
 S3 documents bucket                 S3 Vectors index
 (KMS, versioned)                  (1024-dim cosine default)
        │
        └─ EventBridge Object Created ─► ingest Lambda
                                           │ direct per-document ingest
                                           ▼
                                    Bedrock Knowledge Base

 EventBridge rate(1 minute) ─► reconciler Lambda
                               ├─ polls per-document status in batches
                               └─ expires abandoned uploads
```

API Gateway HTTP API is used for ordinary JSON endpoints and its managed Cognito
authorizer. HTTP API cannot stream responses, so chat uses a Lambda Function URL in
`RESPONSE_STREAM` mode and verifies the Cognito access token in code.

## Repository layout

```text
backend/   TypeScript Lambda handlers, Bedrock agent loop, auth, storage helpers
frontend/  React 19 + Vite single-page application
infra/     Four AWS CDK v2 stacks: storage, knowledge base, auth, and API
shared/    Type-only API and SSE contracts
evals/     Python 3.9 golden-dataset schema scaffold (no Stage 1 eval runner)
samples/   Three Chinese Markdown knowledge-base fixtures
```

## Prerequisites

- Node.js 24 and npm 11
- Python 3.9 or newer
- AWS CLI v2
- AWS CDK v2
- An AWS account with Amazon Bedrock model access enabled in the target region

The chat model context value is a regional cross-region inference-profile ID. Confirm an
available Anthropic profile before deployment:

```bash
aws bedrock list-inference-profiles --region <region> \
  --query "inferenceProfileSummaries[?contains(inferenceProfileId,'anthropic')].inferenceProfileId"
```

Update `chatModelId` in `infra/cdk.json` or pass `-c chatModelId=...`. Model IDs are CDK
context values and are never hardcoded in Lambda source.

`esbuild` is an intentional pinned direct dependency at both the root and in `infra/`.
`NodejsFunction` otherwise falls back to Docker bundling during synth when it cannot
resolve a local copy.

## Local development and verification

Install the JavaScript and Python test environments once:

```bash
npm ci
python3 -m venv .venv
.venv/bin/python -m pip install -e './evals[test]'
```

Run the complete gate:

```bash
npm run verify
```

It performs strict TypeScript checking, zero-warning ESLint, all Vitest suites, the
frontend production build, the infrastructure build and offline CDK synth, then the
Python 3.9 dataset tests. Tests mock every AWS SDK call and do not resolve AWS
credentials.

For local browser development, copy `.env.example` to `frontend/.env`, fill in the six
values from a deployed stack, and run:

```bash
npm run -w frontend dev
```

## Deployment

Deployment is intentionally manual in Stage 1. Review the synthesized templates first,
then run these commands from an authenticated AWS environment:

```bash
npx -w infra cdk bootstrap
npx -w infra cdk deploy --all

aws cognito-idp admin-create-user \
  --user-pool-id <user-pool-id> \
  --username <email> \
  --user-attributes Name=email,Value=<email> Name=email_verified,Value=true
```

Copy `.env.example` to `frontend/.env` and populate it with the CDK outputs and Cognito
domain:

```text
VITE_USER_POOL_ID
VITE_USER_POOL_CLIENT_ID
VITE_COGNITO_DOMAIN
VITE_API_URL
VITE_CHAT_URL
VITE_AWS_REGION
```

Then build and host `frontend/dist/` on the origin configured by
`frontendOrigin`. The default origin is `http://localhost:5173`.

## Security model

- Documents are organization-shared. Any authenticated user can list, retrieve, and
  download any ingested document. `uploaderSub` is retained for audit, not authorization.
- Conversations are private per user. Every detail read gets the session `META` item and
  verifies its stored `userSub` before any message query.
- API Gateway requires Cognito access tokens carrying `kb-api/access`; ID tokens do not
  carry that scope and are rejected.
- The chat Function URL is publicly invokable because browsers cannot SigV4-sign it. Its
  handler verifies the Cognito access token before opening the response stream. CORS is
  not access control. Reserved concurrency limits simultaneous unauthenticated
  invocations; the invocation alarm is observational and does not stop spend.
- S3 and DynamoDB use the customer-managed KMS key. Authored Lambda policies use named
  resources and narrowly scoped actions.
- JWTs, presigned URLs, and full document text are not logged.

The root `.env` belongs to the superseded OpenAI/Voyage implementation and contains
now-unused keys. Rotate those keys and delete that local file when convenient; nothing in
this implementation reads it.

## Cost and cleanup

S3 Vectors, DynamoDB on-demand, and Lambda keep idle cost to roughly a few dollars per
month for a small study environment. Bedrock embeddings and chat inference are billed by
usage/token. KMS, logs, S3 storage, and Cognito may add small usage-based charges.

Remove a deployed study environment with:

```bash
npx -w infra cdk destroy --all
```

The stacks use `DESTROY` removal policies for study convenience; do not copy that data
retention posture into a production system without review.

## Stage 2 roadmap

Stage 1 deliberately does not implement the following:

- Bedrock Guardrails and PII detection/masking
- department/date metadata filtering
- hybrid retrieval and reranking
- Step Functions and human approval
- token, latency, cost dashboards
- a golden-dataset regression runner
- two-model comparison
- WAF, custom domains, CI/CD, multi-region, or VPC/PrivateLink

The forward-compatible hooks already exist: Cognito declares the `department` attribute;
uploads write `documentId`, `uploaderSub`, and numeric `uploadedAt` sidecar metadata;
the retrieval call marks the future filter location; the tool loop can accept additional
tools and an approval gate; and `evals/` contains validated real records ready for a
future runner.
