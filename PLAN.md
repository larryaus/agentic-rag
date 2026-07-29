# Refactor Plan — Enterprise Knowledge Base AI Assistant on AWS (Stage 1)

**Revision 3.** Revised after two independent design-review passes. Sections tagged **[R2]**
changed in the first revision, **[R3]** in the second. If you have seen an earlier copy of
this plan, discard it — several **[R3]** items reverse instructions given earlier.

## 0. Context and mandate

The repository at `/Users/Larry/GitHub/agentic-rag` currently contains a Python 3.11
FastAPI service (~1,875 LOC) implementing agentic RAG with OpenAI + Voyage embeddings +
Postgres/pgvector, plus a vanilla-JS single page at `web/`. It has zero AWS code, no auth,
no logging, no IaC, and 8 pytest tests.

We are replacing it with an AWS-native **Enterprise Knowledge Base AI Assistant**. This
document specifies **Stage 1 only**. Stage 2 (Guardrails, PII masking, department/date
metadata filters, hybrid search + reranker, Step Functions human approval, cost
dashboards, golden-dataset eval runner, two-model comparison) is explicitly **out of
scope** and must NOT be built now.

The purpose is **AWS AI-certification exam preparation and portfolio demonstration**.
Optimize for clear mapping to exam domains, low idle cost, and readable code. Do not
optimize for production hardening (no WAF, no multi-region, no blue/green, no CI/CD).

### Non-negotiable constraints

1. **No live AWS deployment.** Do not run `cdk deploy`, `cdk bootstrap`, or anything that
   mutates a real AWS account. `cdk synth` must succeed offline with no credentials.
2. **All AWS SDK calls in tests must be mocked.** No test may perform a network call or
   trigger credential resolution.
3. **Python on this machine is 3.9.13 only.** The `evals/` package must run on 3.9. Every
   Python module must begin with `from __future__ import annotations` so PEP 604
   (`str | None`) annotations do not raise `TypeError` at import. This exact bug currently
   breaks the repo's suite on this machine — do not reintroduce it.
4. **Do not hardcode Bedrock model IDs in source.** They are region-scoped inference
   profile IDs that change. They are CDK context values passed to Lambdas as env vars (§6).
5. **Docker is not running on this machine.** Anything that requires Docker at synth time
   will fail. See §3.5 — a pinned local `esbuild` is mandatory, not optional.
6. Node 24 / npm 11 are installed. Use them. Do not add pnpm/yarn/bun.

### [R2] Settled design decisions — do not relitigate

These were decided in review. Implement them as written.

| Decision | Choice | Why |
|---|---|---|
| Document visibility | **Org-shared.** Any authenticated user can retrieve any ingested document. | This is an *enterprise* knowledge base — the handbook and FAQ are meant to be shared. Per-document access control is Stage 2's metadata-filter story. |
| Conversation visibility | **Per-user private.** A user may never read another user's session or message. | Conversations are personal; this is the real tenancy boundary in Stage 1. |
| S3 → ingest trigger | **EventBridge**, not S3 bucket notifications. | Bucket notifications force the bucket to reference the Lambda, creating a cross-stack cycle and pulling in CDK's notification custom resource. `eventBridgeEnabled: true` is a plain bucket property, so the dependency flows one way. |
| Ingestion | **`IngestKnowledgeBaseDocuments` per document**, polled to a terminal state by a 1-minute reconciler using `GetKnowledgeBaseDocuments`. **[R3]** | Data-source sync jobs (`StartIngestionJob`) are whole-source, single-concurrency, and report only aggregate counts — they cannot tell you which upload failed. Direct per-document ingestion gives per-document status natively and removes the start-race entirely. |
| API token type | **Access token** (`tokenUse: 'access'`). | Cognito access tokens are the API-authorization token; ID tokens are identity claims. Stage 1 needs no custom attributes at the API boundary. |

---

## 1. Target architecture **[R2]**

```
Browser (React 19 + TS, Vite)
  │
  ├── Cognito Hosted UI (auth code + PKCE + state + nonce) ──► Cognito User Pool
  │
  ├── HTTPS + access token ─► API Gateway HTTP API (HttpUserPoolAuthorizer)
  │      ├─ POST /v1/uploads                    → presign Lambda   → S3 presigned PUT
  │      ├─ GET  /v1/documents                  → documents Lambda → DynamoDB
  │      ├─ GET  /v1/documents/{id}/download    → documents Lambda → S3 presigned GET
  │      ├─ GET  /v1/sessions                   → sessions Lambda  → DynamoDB
  │      └─ GET  /v1/sessions/{sessionId}       → sessions Lambda  → DynamoDB
  │
  └── HTTPS + access token ─► Lambda Function URL (RESPONSE_STREAM)
                                └─ chat Lambda (SSE) ─► Bedrock Retrieve + ConverseStream
                                                     ─► DynamoDB (history)

S3 docs bucket (eventBridgeEnabled)
      └─(EventBridge: Object Created)─► ingest Lambda
                                          ├─ skip *.metadata.json sidecar events
                                          ├─ oversize → delete object + sidecar, mark FAILED
                                          └─ UPLOADING→PENDING→INGESTING via
                                             IngestKnowledgeBaseDocuments (one document)
EventBridge rule (rate 1 min) ─────────► reconciler Lambda  [poll only, never starts work]
                                          ├─ GetKnowledgeBaseDocuments (batches of 10)
                                          │    INDEXED→READY, terminal→FAILED
                                          └─ UPLOADING older than 10 min → FAILED + sweep sidecar

Bedrock Knowledge Base ──► S3 Vectors (vector bucket + index, 1024-dim, cosine)
                       ──► amazon.titan-embed-text-v2:0
```

**Why two API surfaces:** API Gateway **HTTP API** cannot stream responses, so SSE requires
a Lambda Function URL with `invokeMode: RESPONSE_STREAM`. (API Gateway *REST* APIs have a
separate streaming mode; we are not using REST APIs. Word any comment as "HTTP API cannot
stream," not "API Gateway cannot stream.") This split also has study value: the HTTP API
demonstrates the managed Cognito authorizer, the Function URL demonstrates in-code JWT
verification.

---

## 2. Repository layout after the refactor

npm workspaces monorepo. Root `package.json` declares `["shared", "backend", "infra", "frontend"]`.

```
/
├── package.json                 # workspaces root + scripts (§7)
├── package-lock.json            # MUST be committed — `npm ci` requires it
├── tsconfig.base.json
├── eslint.config.js             # flat config, typescript-eslint
├── .gitignore                   # add cdk.out/, dist/, .venv/
├── .env.example
├── README.md                    # rewritten in English (§9)
│
├── shared/            # @kb/shared — types only, zero runtime deps
│   └── src/{index,api,sse}.ts
│
├── backend/           # @kb/backend
│   └── src/
│       ├── handlers/{chat,ingest,reconciler,presign,documents,sessions}.ts
│       ├── lib/{config,clients,auth,ddb,retrieve,agent,tools,prompts,
│       │        citations,sse,errors,logger,presigner}.ts
│       └── __tests__/
│
├── infra/             # @kb/infra — CDK v2
│   ├── cdk.json
│   ├── bin/app.ts
│   ├── lib/{storage,knowledge-base,auth,api}-stack.ts
│   └── test/stacks.test.ts
│
├── frontend/          # @kb/frontend — React 19 + Vite
│   └── src/{main.tsx,App.tsx,auth/,api/,components/,styles.css,__tests__/}
│
└── evals/             # Python 3.9+ — SCAFFOLD ONLY in Stage 1
    ├── pyproject.toml
    ├── src/kb_evals/{__init__,dataset,models}.py
    ├── datasets/golden.json
    └── tests/test_dataset.py
```

### Files to delete **[R2]**

Tracked paths — remove with `git rm -r`:

```
src/agent_service/   migrations/   web/   tests/   scripts/
entrypoint.sh   docker-compose.yml   Dockerfile   pyproject.toml
```

**`samples/` is NOT in that list. Do not delete it.** The three Chinese Markdown documents
there become KB fixtures and the source material for the golden dataset.

`tests/` currently holds five files (`__init__.py`, `conftest.py`, `test_chunker.py`,
`test_agent_loop.py`, `test_retriever.py`). All go.

`src/agent_service.egg-info/` is **untracked** (matched by `*.egg-info/` in `.gitignore`),
so `git rm` cannot stage it. Delete it with a plain `rm -rf`.

`samples/*.md` (3 Chinese Markdown docs) are **kept** — they become KB fixtures and the
source material for the golden dataset.

**`.env` at the repo root holds real-looking `OPENAI_API_KEY` and `VOYAGE_API_KEY` values.**
It is gitignored and untracked. Do **not** delete it, do **not** commit it, do **not** read
its values into any file. The README tells the user to rotate those keys and delete the file.

---

## 3. Infrastructure (CDK)

CDK v2 TypeScript. Four stacks in one app: `KbStorageStack`, `KbKnowledgeBaseStack`,
`KbAuthStack`, `KbApiStack`. Pass resources between stacks via **direct construct
references in props**, never `Fn::ImportValue` strings.

Dependency order is strictly one-way: `Storage → KnowledgeBase → Api`, and `Auth → Api`.
Nothing in `Storage` may reference anything in `Api`. **[R2]**

`bin/app.ts` must set an explicit env so synth works with no credentials:
```ts
env: {
  account: process.env.CDK_DEFAULT_ACCOUNT ?? '123456789012',
  region:  process.env.CDK_DEFAULT_REGION  ?? 'us-east-1',
}
```

### 3.1 `storage-stack.ts`

- **KMS key** `DataKey`: `enableKeyRotation: true`, alias `alias/kb-assistant`.
- **S3 bucket** `DocumentsBucket`: KMS encryption with the above key,
  `blockPublicAccess: BLOCK_ALL`, `enforceSSL: true`, `versioned: true`,
  `removalPolicy: DESTROY`, `autoDeleteObjects: true`,
  **`eventBridgeEnabled: true`** **[R2]**, and a CORS rule allowing `PUT` from the
  `frontendOrigin` context value with `allowedHeaders: ['*']`.
- **S3 Vectors** (L1s from `aws-cdk-lib/aws-s3vectors`):
  ```ts
  const vectorBucket = new s3vectors.CfnVectorBucket(this, 'VectorBucket', {
    vectorBucketName: `kb-vectors-${this.account}-${this.region}`,
  });
  const vectorIndex = new s3vectors.CfnIndex(this, 'VectorIndex', {
    indexName: 'kb-index',
    vectorBucketArn: vectorBucket.attrVectorBucketArn,
    dataType: 'float32',
    dimension: props.embeddingDimension,   // [R2] from context, not a literal
    distanceMetric: 'cosine',
  });
  ```
  Expose `attrVectorBucketArn` and `attrIndexArn` as readonly stack properties.
- **DynamoDB table** `ConversationsTable`: `pk`/`sk` STRING, `PAY_PER_REQUEST`,
  `TableEncryption.CUSTOMER_MANAGED` with the key, PITR enabled,
  `timeToLiveAttribute: 'ttl'`, `removalPolicy: DESTROY`, GSI `gsi1`
  (`gsi1pk` STRING / `gsi1sk` STRING, `projectionType: ALL`).

#### [R2] DynamoDB item shapes — entity-typed GSI keys

The GSI partition key **must** carry the entity type, otherwise `GET /v1/sessions` and
`GET /v1/documents` query the same partition and need a post-read `FilterExpression`,
which breaks pagination.

| Entity | pk | sk | gsi1pk | gsi1sk | Attributes |
|---|---|---|---|---|---|
| Session meta | `SESSION#<sessionId>` | `META` | `USER#<sub>#SESSION` | `<updatedAt ISO>` | `userSub`, `title`, `createdAt`, `updatedAt`, `messageCount`, `ttl` |
| Message | `SESSION#<sessionId>` | `MSG#<createdAt ISO>#<uuid>` | — | — | `userSub`, `role`, `content`, `citations`, `usage`, `createdAt`, `ttl` |
| Document | `DOC#<documentId>` | `META` | `ORG#DOCUMENT` | `<uploadedAt ISO>` | `documentId`, `uploaderSub`, `title`, `s3Key`, `contentType`, `sizeBytes`, `status`, `ingestionJobId`, `errorMessage`, `uploadedAt`, `updatedAt` |

Notes, all mandatory:
- **`userSub` is stored as a real attribute** on session and message items, not merely
  derived from `gsi1pk`. Ownership checks read the attribute. **[R2]**
- Document `gsi1pk` is the constant `ORG#DOCUMENT` because documents are org-shared
  (§0). The uploader is recorded in `uploaderSub` for audit only. **[R2]**
- **`ttl` is set on session and message items ONLY (90 days).** Document items have **no
  TTL** — expiring the DynamoDB row while the S3 object and its indexed vectors remain
  would orphan retrievable content with no metadata. **[R2]**
- `sessionId`/`documentId` are UUID v4.
- `status` ∈ `PENDING` | `INGESTING` | `READY` | `FAILED`.

### 3.2 `knowledge-base-stack.ts`

No L2 exists. Use `CfnKnowledgeBase` / `CfnDataSource` from `aws-cdk-lib/aws-bedrock`.

- **`KnowledgeBaseRole`**: `assumedBy: new iam.ServicePrincipal('bedrock.amazonaws.com')`
  with a `StringEquals` condition on `aws:SourceAccount` and `ArnLike` on `aws:SourceArn`
  (`arn:aws:bedrock:<region>:<account>:knowledge-base/*`) to close the confused-deputy hole.
  Policies:
  - `bedrock:InvokeModel` on `arn:aws:bedrock:<region>::foundation-model/<embeddingModelId>`
  - `s3:GetObject` on `<bucket>/*`, `s3:ListBucket` on `<bucket>`
  - `kms:Decrypt` on the data key
  - **exactly** `s3vectors:PutVectors`, `GetVectors`, `DeleteVectors`, `QueryVectors`,
    `GetIndex` on the index ARN. **Do not add `ListVectors`** — it is not in Bedrock's
    documented required set. **[R2]**
- **`CfnKnowledgeBase`** — `name` is **required** and was missing in revision 1: **[R2]**
  ```ts
  const kb = new bedrock.CfnKnowledgeBase(this, 'KnowledgeBase', {
    name: `kb-assistant-${this.account}`,          // required
    roleArn: kbRole.roleArn,
    knowledgeBaseConfiguration: {
      type: 'VECTOR',
      vectorKnowledgeBaseConfiguration: {
        embeddingModelArn:
          `arn:aws:bedrock:${this.region}::foundation-model/${props.embeddingModelId}`,
        // [R3] Without this, a 512/256 override resizes the S3 vector index while Titan
        // keeps emitting 1024-dim vectors, and ingestion fails at runtime.
        embeddingModelConfiguration: {
          bedrockEmbeddingModelConfiguration: {
            dimensions: props.embeddingDimension,
            embeddingDataType: 'FLOAT32',
          },
        },
      },
    },
    storageConfiguration: {
      type: 'S3_VECTORS',
      s3VectorsConfiguration: {
        indexArn: props.vectorIndexArn,
        vectorBucketArn: props.vectorBucketArn,
      },
    },
  });
  kb.node.addDependency(kbRole);   // and the role's default policy
  ```
- **`CfnDataSource`** — `name` and `knowledgeBaseId` are **required** and were missing: **[R2]**
  ```ts
  new bedrock.CfnDataSource(this, 'DataSource', {
    name: 'documents-s3',                          // required
    knowledgeBaseId: kb.attrKnowledgeBaseId,       // required
    dataSourceConfiguration: {
      type: 'S3',
      s3Configuration: { bucketArn: props.documentsBucket.bucketArn },
    },
    vectorIngestionConfiguration: {
      chunkingConfiguration: {
        chunkingStrategy: 'FIXED_SIZE',
        fixedSizeChunkingConfiguration: { maxTokens: 512, overlapPercentage: 20 },
      },
    },
    dataDeletionPolicy: 'DELETE',
  });
  ```
  512 tokens matches the old `CHUNK_TOKENS=512`. **20% overlap is a deliberate increase**
  from the old 64-token (12.5%) setting — Bedrock's `overlapPercentage` minimum granularity
  and better recall on Chinese text justify it. Say exactly that in the comment; do not
  claim it "mirrors" the old value. **[R2]**

Export `attrKnowledgeBaseId` and `attrDataSourceId`.

### 3.3 `auth-stack.ts`

- **`UserPool`** `KbUserPool`: `selfSignUpEnabled: false` (admin-created users only),
  `signInAliases: { email: true }`, `standardAttributes.email` required + immutable,
  `passwordPolicy.minLength: 12`, `mfa: Mfa.OPTIONAL` with TOTP,
  `accountRecovery: EMAIL_ONLY`, `removalPolicy: DESTROY`.
- **Custom attribute** — declare with schema name **`department`** (Cognito exposes it in
  tokens as `custom:department`). Mutable string, max 64.
  Comment must say: *custom attributes cannot be removed or have their type changed once
  defined, so declare it now; Stage 2 metadata filtering depends on it.* Do **not** claim
  attributes cannot be added later — they can. **[R2]**
- **`UserPoolClient`** `WebClient`: `generateSecret: false`, `authFlows: { userSrp: true }`,
  `oAuth.flows.authorizationCodeGrant: true`, scopes `[OPENID, EMAIL, PROFILE]`,
  `callbackUrls: [\`\${frontendOrigin}/callback\`]`, `logoutUrls: [frontendOrigin]`,
  access/id token validity 1 hour, refresh 30 days, `preventUserExistenceErrors: true`.
- **`UserPoolDomain`**: prefix from context `cognitoDomainPrefix`; **if that value is empty,
  derive `kb-assistant-${account}`**. An empty string is not a valid prefix. **[R2]**
- `CfnOutput`: user pool ID, client ID, domain prefix, region.

### 3.4 `api-stack.ts`

All Lambdas: `NodejsFunction`, `Runtime.NODEJS_22_X`, `Architecture.ARM_64`,
`tracing: Tracing.ACTIVE`, dedicated `LogGroup` (`retention: ONE_WEEK`,
`removalPolicy: DESTROY`), `bundling: { minify: true, sourceMap: true, format: OutputFormat.ESM }`,
env `NODE_OPTIONS: '--enable-source-maps'`.

| Lambda | Handler | Mem | Timeout | Trigger |
|---|---|---|---|---|
| `ChatFunction` | `handlers/chat.ts` | 1024 | 5 min | Function URL (`RESPONSE_STREAM`), `reservedConcurrentExecutions: 5` **[R2]** |
| `IngestFunction` | `handlers/ingest.ts` | 512 | 1 min | EventBridge rule: S3 `Object Created` on the bucket **[R2]** |
| `ReconcilerFunction` **[R2]** | `handlers/reconciler.ts` | 512 | 2 min | EventBridge `Rule` with `Schedule.rate(Duration.minutes(1))` |
| `PresignFunction` | `handlers/presign.ts` | 256 | 15 s | HTTP API `POST /v1/uploads` |
| `DocumentsFunction` | `handlers/documents.ts` | 256 | 15 s | `GET /v1/documents`, `GET /v1/documents/{documentId}/download` |
| `SessionsFunction` | `handlers/sessions.ts` | 256 | 15 s | `GET /v1/sessions`, `GET /v1/sessions/{sessionId}` |

- **HTTP API**: `apigatewayv2.HttpApi` with `corsPreflight` (origin = `frontendOrigin`,
  methods `GET,POST,OPTIONS`, headers `authorization,content-type`). Protect every route
  with **`HttpUserPoolAuthorizer`** from `aws-cdk-lib/aws-apigatewayv2-authorizers`,
  constructed with the user pool and client — purpose-built for Cognito, and it handles the
  fact that Cognito **access** tokens carry `client_id` rather than `aud`.
- **[R3] Enforce access-tokens-only with a scope.** A bare JWT authorizer accepts a Cognito
  **ID** token too, which would contradict §4.2's `tokenUse: 'access'`. Close it:
  add a `UserPoolResourceServer` with identifier `kb-api` and a scope `access`, include
  `kb-api/access` in the client's `oAuth.scopes`, and set
  `authorizationScopes: ['kb-api/access']` on **every** protected route. ID tokens carry no
  `scope` claim and are then rejected at the gateway. Assert the scope on each synthesized
  `AWS::ApiGatewayV2::Route` in the infra test.
- **Function URL**: `authType: FunctionUrlAuthType.NONE`, `invokeMode: RESPONSE_STREAM`,
  CORS restricted to `frontendOrigin`, methods `[POST]`.
  Required comment, verbatim in substance: *`authType: NONE` is required because browsers
  cannot SigV4-sign a request. The handler verifies the Cognito access token itself. This
  URL is publicly invokable — CORS is a browser convention, not access control. Reserved
  concurrency caps how many unauthenticated requests can execute at once; the
  invocation-count alarm is observational only and does not itself stop spend.* **[R3]**
  (Revision 2 claimed the alarm "bounds abuse cost" — it does not; only the concurrency cap
  constrains anything.)
- **IAM (no `Resource: '*'` in any policy we author):**
  - Chat: `bedrock:Retrieve` on the KB ARN; `bedrock:InvokeModelWithResponseStream` +
    `bedrock:InvokeModel` on **both** the inference-profile ARN and the underlying
    foundation-model ARNs (§6); `dynamodb:Query|PutItem|UpdateItem|GetItem` on the table
    and `<table>/index/gsi1`; `kms:Decrypt`, `kms:GenerateDataKey`.
  - Ingest **[R3]**: `bedrock:IngestKnowledgeBaseDocuments` on the KB ARN;
    `dynamodb:UpdateItem|GetItem` on the table; **`s3:DeleteObject` on `<bucket>/uploads/*`**
    (to remove oversized rejects and their sidecars); `kms:Decrypt`, `kms:GenerateDataKey`.
  - Reconciler **[R3]**: `bedrock:GetKnowledgeBaseDocuments` on the KB ARN;
    `dynamodb:Query|UpdateItem` on table+index; `s3:DeleteObject` on `<bucket>/uploads/*`
    (orphaned sidecars); `kms:Decrypt`, `kms:GenerateDataKey`.
    **No role anywhere grants `StartIngestionJob`, `GetIngestionJob`, or
    `ListIngestionJobs`** — the design no longer uses data-source sync jobs.
  - Presign: `s3:PutObject` on `<bucket>/uploads/*`; **`dynamodb:PutItem` on the table**
    (missing in revision 1 — the handler writes the `PENDING` record); `kms:Decrypt`,
    `kms:GenerateDataKey`. **[R2]**
  - Documents: `dynamodb:Query|GetItem` on table+index; **`s3:GetObject` on
    `<bucket>/uploads/*`** (for the presigned download, new in R2); `kms:Decrypt`.
  - Sessions: `dynamodb:Query|GetItem` on table+index; `kms:Decrypt`.
- **Alarms**: `Errors ≥ 1 / 5 min` on chat, ingest, reconciler; `Throttles ≥ 1 / 5 min` on
  chat; **`Invocations ≥ 500 / 5 min` on chat** (abuse guard for the public URL) **[R2]**.
  No SNS action.
- `CfnOutput`: HTTP API URL, Function URL.

### 3.5 [R2] Bundling — mandatory, or synth fails

`NodejsFunction` bundles at synth time. It uses a **local** `esbuild` when one is
resolvable and otherwise falls back to **Docker**, which is not running on this machine.

Therefore: add **`esbuild` as a pinned devDependency** in `infra/package.json` (and at the
workspace root so hoisting resolves it), and commit `package-lock.json`. Without this,
`npm run verify` fails at `cdk synth` on a clean checkout. Add a comment in
`infra/package.json` context or the README noting why esbuild is a direct dependency.

---

## 4. Backend implementation

### 4.1 [R2] Per-handler configuration

Revision 1 required every env var in one `loadConfig()`, which contradicts least privilege
(the sessions Lambda has no bucket, the ingest Lambda has no Cognito client).

`lib/config.ts` exports small, composable readers plus **one loader per handler**, each
validating only what that handler needs and throwing at module load if absent:

| Loader | Reads (authored env vars only) |
|---|---|
| `loadChatConfig()` | `TABLE_NAME`, `KNOWLEDGE_BASE_ID`, `CHAT_MODEL_ID`, `USER_POOL_ID`, `USER_POOL_CLIENT_ID`, `RETRIEVAL_TOP_K`(8), `MAX_TOOL_ITERATIONS`(6), `MAX_HISTORY_MESSAGES`(20), `SESSION_TTL_DAYS`(90) |
| `loadIngestConfig()` | `TABLE_NAME`, `KNOWLEDGE_BASE_ID`, `DATA_SOURCE_ID`, `DOCS_BUCKET`, `MAX_UPLOAD_BYTES`(26214400) |
| `loadReconcilerConfig()` | `TABLE_NAME`, `KNOWLEDGE_BASE_ID`, `DATA_SOURCE_ID`, `DOCS_BUCKET`, `ABANDONED_UPLOAD_MINUTES`(10) |
| `loadPresignConfig()` | `TABLE_NAME`, `DOCS_BUCKET`, `MAX_UPLOAD_BYTES`(26214400) |
| `loadDocumentsConfig()` | `TABLE_NAME`, `DOCS_BUCKET` |
| `loadSessionsConfig()` | `TABLE_NAME` |

`api-stack.ts` must set **exactly** these variables on each function — no extras.

> **`AWS_REGION` is a reserved Lambda environment variable.** Putting it in a function's
> CDK `environment` map fails deployment with a reserved-key error. It is **absent from
> every row above by design.** Runtime code that needs the region reads the
> runtime-provided `process.env.AWS_REGION` directly (or lets the SDK default to it); no
> loader validates it and no stack sets it. Add an infra test asserting that no authored
> function environment contains `AWS_REGION`.

### 4.2 [R2] `lib/auth.ts` — access tokens

Verifier created **once at module scope** (per-request creation re-fetches the JWKS):

```ts
const verifier = CognitoJwtVerifier.create({
  userPoolId: cfg.userPoolId,
  tokenUse: 'access',          // [R2] access, not id
  clientId: cfg.userPoolClientId,
});
export async function verifyBearer(header: string | undefined): Promise<AuthContext>
```

`AuthContext = { sub: string; username: string }`. Throw `UnauthorizedError` for a missing
header, a non-`Bearer ` prefix, or a rejected token.

Stage 1 does **not** read `custom:department` — access tokens do not carry custom
attributes. Leave a comment at the `AuthContext` definition: *Stage 2 department filtering
will resolve department from a user-profile item in DynamoDB or a Cognito `GetUser` call,
not from the access token.*

For HTTP API handlers the authorizer has already validated the token — read
`event.requestContext.authorizer.jwt.claims.sub` and do **not** re-verify. Only the chat
Function URL calls `verifyBearer`. Both paths produce the same `AuthContext` type.

### 4.3 `lib/retrieve.ts`

```ts
export async function retrieve(opts: { query: string; topK: number }): Promise<RawChunk[]>
```
`BedrockAgentRuntimeClient` → `RetrieveCommand`, with
`retrievalConfiguration.vectorSearchConfiguration.numberOfResults = topK`.

```ts
type RawChunk = {
  text: string;        // result.content?.text ?? ''
  sourceUri: string;   // result.location?.s3Location?.uri ?? ''
  documentId: string;  // result.metadata?.documentId — see below
  title: string;       // decodeURIComponent(basename(sourceUri))
  score: number;       // result.score ?? 0
};
```

**[R3] `documentId` comes from the sidecar metadata, not from parsing the S3 URI.** Read
`result.metadata?.documentId` and validate it is a well-formed UUID. Revision 2 parsed the
UUID out of the key path and fell back to `''`, which produces a broken
`/v1/documents//download` link. Do not confuse this application-owned UUID with Bedrock's
own internal document identifier — they are different values. **If the attribute is absent
or not a UUID, drop the chunk from the citation index** (it can still be passed to the
model as context, but it must never become a clickable citation).

**No `ref` field here.** Reference numbers are assigned by the agent loop across the whole
turn (§4.4), not per retrieval call. **[R2]**

Leave exactly this comment at the call site:
`// Stage 2: retrievalConfiguration.vectorSearchConfiguration.filter goes here (department/date).`

### 4.4 [R2] `lib/agent.ts` — the ConverseStream tool loop

Use **`Retrieve` + `ConverseStream` with a `search_knowledge_base` tool**, not
`RetrieveAndGenerate`. Comment the reason: this keeps the agentic tool-calling loop, into
which Stage 2's external-API tool and human-approval gate plug directly;
`RetrieveAndGenerate` is a closed box that would have to be torn out.

**Tool definition** (`lib/tools.ts`):
```ts
{ toolSpec: {
    name: 'search_knowledge_base',
    description: 'Search the enterprise knowledge base for passages relevant to a question. Always call this before making any factual claim about company documents.',
    inputSchema: { json: {
      type: 'object',
      properties: { query: { type: 'string', description: 'A focused standalone search query. Strip pleasantries and pronouns.' } },
      required: ['query'],
    }},
}}
```

**Signature and responsibilities.** The agent emits streaming events and *returns* a
result; it does **not** write to DynamoDB and does **not** emit the `done` frame. The
handler owns persistence and `done`. (Revision 1 split this ambiguously.)

```ts
export async function runAgent(opts: {
  history: Message[];
  userMessage: string;
  emit: (event: SseEvent) => void;   // tool_use | citation | text
  topK: number;
  maxIterations: number;
  modelId: string;
}): Promise<{
  text: string;
  citations: Citation[];
  usage: { inputTokens: number; outputTokens: number };
  stopReason: string;                // includes the synthetic 'max_iterations'
}>
```

**Reference numbering — monotonic across the turn.** Maintain one counter and one
`Map<number, RawChunk>` for the entire `runAgent` call. Each retrieved chunk gets the next
integer, starting at 1. **Do not restart numbering per tool call** — with up to 6
iterations, per-call numbering makes a later `[ref:1]` overwrite an earlier one and
citations resolve to the wrong document. **[R2]**

**Loop, per iteration:**
1. Send `ConverseStreamCommand` with `modelId`, `messages`, `system`, `toolConfig`,
   `inferenceConfig: { maxTokens: 2048 }`.
2. Consume `response.stream`. Handle:
   - `messageStart`
   - `contentBlockStart` — if `start.toolUse`, record `{ toolUseId, name }` **keyed by
     `contentBlockIndex`**. Capturing `toolUseId` is mandatory; the `toolResult` is
     rejected without it.
   - `contentBlockDelta` — `delta.text` → append to the text accumulator for that index and
     `emit({ type: 'text', delta })`; `delta.toolUse.input` → append the **string fragment**
     to that index's buffer.
   - `contentBlockStop` — if the index is a tool block, `JSON.parse` the **concatenated**
     buffer now. Parsing an individual fragment throws; this is the single most likely
     implementation bug.
   - `messageStop` — read `stopReason`.
   - `metadata` — accumulate `usage.inputTokens` / `usage.outputTokens`.
   - **Exception events** — `internalServerException`, `modelStreamErrorException`,
     `validationException`, `throttlingException`, `serviceUnavailableException`. These
     arrive *after* the HTTP request succeeded, so a plain try/catch around `send()` misses
     them. **[R3] The partial-result contract, stated once:** throw a
     `BedrockStreamError` that **carries a complete `partialResult`** on the error object —
     `{ text, citations, usage, stopReason: 'error' }` — because a thrown error otherwise
     discards everything accumulated. The handler catches it, **persists `partialResult` as
     the assistant message**, then emits the sanitized SSE `error` frame. Add `stopReason`
     to the message item attributes (§3.1). The handler also **persists the user message
     before invoking Bedrock**, so a timeout never loses the submitted turn.
3. **Reconstruct and append the assistant turn.** Build the complete assistant `Message`
   from the accumulated blocks *in content-block-index order* — text blocks as
   `{ text }`, tool blocks as `{ toolUse: { toolUseId, name, input } }` — and push it onto
   `messages`. A `toolResult` cannot be appended as a free-standing block; Bedrock requires
   the assistant turn it answers to be present. **[R2]**
4. If `stopReason !== 'tool_use'`, break.
   **[R3] On the final permitted iteration, do not dispatch tools even if the model asked.**
   The budget counts model iterations, so there is no iteration left to consume the results
   — running them would burn a `Retrieve` call whose output is discarded. Break immediately
   with `stopReason: 'max_iterations'`. The corresponding test asserts the number of
   `retrieve` **dispatches**, not just the number of model calls.
5. **Handle every tool-use block in the turn, not just one** — Converse may return
   several. For each: `emit({type:'tool_use'})`, run it, assign refs, emit one `citation`
   per chunk. Collect all results and append **one** `user` message containing **all**
   `toolResult` blocks: **[R2]**
   ```ts
   messages.push({ role: 'user', content: results.map(r => ({ toolResult: r })) });
   ```
   Each `toolResult` is `{ toolUseId, status: 'success', content: [{ json: { results: [...] } }] }`
   where each entry has `ref`, `title`, `documentId`, `score`, and `text` truncated to 1500 chars.
6. **Tool errors never throw out of the loop.** Catch and return
   `{ toolUseId, status: 'error', content: [{ text: \`\${err.name}: \${err.message}\` }] }`
   so the model can recover. This preserves the old code's deliberate behavior.
7. An unknown tool name gets the same `status: 'error'` treatment.

**Budget — one number, not two. [R2]** `MAX_TOOL_ITERATIONS = 6` counts **model
iterations** (loop passes), not individual tool calls. The system prompt's guidance is
softened to match so the two no longer contradict. On exhaustion, return
`stopReason: 'max_iterations'` and `text` set to whatever was accumulated, or — if empty —
the literal fallback `"I could not complete this question within the allowed number of
search steps. Please try narrowing it."`.

**History is bounded. [R2]** The handler loads at most `MAX_HISTORY_MESSAGES` (20) most
recent messages via a `Query` with `ScanIndexForward: false` and `Limit`, then reverses
them into chronological order. This bounds both the DynamoDB 1 MB page limit and the model
context. Document the ordering explicitly.

**[R3] Strip `[ref:N]` markers from historical assistant messages before sending them to
Bedrock.** Ref numbering restarts at 1 each turn, so replayed history containing an old
`[ref:1]` collides with this turn's `[ref:1]` and invites the model to reuse a stale marker.
Run the stored text through `text.replace(CITATION_RE, '')` when mapping history to
`Message[]`. Store the markers unmodified in DynamoDB — the UI needs them. Cover this with a
two-turn test.

### 4.5 [R2] System prompt (`lib/prompts.ts`) — English

```
You are an enterprise knowledge base assistant.

Rules:
1. Before making any factual claim about company documents, call search_knowledge_base.
2. Never fabricate. If the knowledge base does not contain the answer, say so plainly.
3. Cite every factual sentence with the marker of the passage supporting it, in the exact
   form [ref:N], where N is the `ref` value from a tool result. Put the marker at the end
   of the sentence. Never invent a ref number you were not given.
4. Reply in the same language the user wrote in.
5. Break complex questions into focused searches. Prefer few, well-targeted searches over
   many broad ones.
```

Rule 5 replaces revision 1's "at most 4 tool calls per turn", which contradicted the
6-iteration code limit.

### 4.6 `lib/citations.ts`

- `CITATION_RE = /\[ref:(\d+)\]/g` (reset `lastIndex`, or use `matchAll`).
- `extractRefs(text): number[]` — deduped, in order of appearance.
- `resolveCitations(text, index: Map<number, RawChunk>): Citation[]` — returns only refs
  present in the index, so a hallucinated `[ref:99]` is silently dropped.
- `Citation = { ref, title, documentId, score, snippet }`, `snippet` = first 240 chars.

### 4.7 [R2] `lib/sse.ts` and `handlers/chat.ts`

Frame format: `event: <name>\ndata: <json>\n\n`.

Event union in `shared/src/sse.ts`, consumed by backend and frontend:

| event | data |
|---|---|
| `session` | `{ sessionId }` — always first |
| `tool_use` | `{ name, input }` |
| `citation` | `{ ref, title, documentId, score, snippet }` |
| `text` | `{ delta }` |
| `done` | `{ sessionId, stopReason, usage: { inputTokens, outputTokens } }` |
| `error` | `{ message }` |

**Ordering rule that revision 1 got wrong: validate everything that can produce an HTTP
status code BEFORE committing the response.** Once `HttpResponseStream.from(...)` writes
`statusCode: 200`, a bad token can no longer become a 401. Handler order is:

1. Reject non-`POST` → **405**.
2. `verifyBearer` → on failure **401**.
3. Parse and validate body (`{ sessionId?: string; message: string }`; message non-empty,
   ≤ 8000 chars) → on failure **400**.
4. If `sessionId` is supplied, `GetItem` the `META` row and check `userSub === auth.sub`
   → missing **404**, mismatch **403**.
5. **Only now** commit the streaming response with
   `awslambda.HttpResponseStream.from(responseStream, { statusCode: 200,
   headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' } })`.
6. Emit `session` → `runAgent(...)` → persist the user message, the assistant message, and
   the session `META` update → emit `done`.
7. Any error thrown after step 5 → emit an `error` frame with a **sanitized** message
   (never a raw exception for untyped errors), then end the stream. Always end the stream.

**How a non-200 is actually produced.** A `streamifyResponse` handler must write to and
**end** the stream it was given; returning a plain `{ statusCode }` object does nothing.
Implement a helper and use it for every early exit in steps 1–4:

```ts
async function failFast(responseStream, statusCode: number, message: string) {
  const s = awslambda.HttpResponseStream.from(responseStream, {
    statusCode,
    headers: { 'content-type': 'application/json' },
  });
  s.write(JSON.stringify({ message }));
  s.end();
  await finished(s);          // node:stream/promises
}
```

Each early exit calls `await failFast(...)` and then `return` — the handler always returns
`void`. `chat-handler.test.ts` therefore asserts on the **metadata written to the stream
and that the stream was ended**, not on a returned object.

**Types.** Pin a current `@types/aws-lambda`, which already declares the `awslambda`
global including `HttpResponseStream`. Do **not** hand-write a partial `declare global`
block — revision 1's version declared `streamifyResponse` only and would not have
type-checked the `HttpResponseStream.from` call. **[R2]**

### 4.8 [R2] Upload, ingest, reconcile

**`handlers/presign.ts`** — `POST /v1/uploads`, body `{ filename, contentType, sizeBytes }`.
- **`contentType` and the filename extension must be an allowlisted matching pair. [R3]**
  Bedrock parses by file format, not by declared MIME, so a `.exe` uploaded as
  `text/plain` would reach the parser. Reject anything not in this table:

  | `contentType` | required extension |
  |---|---|
  | `application/pdf` | `.pdf` |
  | `text/plain` | `.txt` |
  | `text/markdown` | `.md` |
  | `text/html` | `.html` or `.htm` |

- `sizeBytes` must be a positive integer ≤ `MAX_UPLOAD_BYTES`.
- Sanitize `filename`: take the basename, strip anything outside `[A-Za-z0-9._-]`, collapse
  repeats, truncate to 128 chars. **Reject** if the result is empty, `.`, `..`, or **ends
  in `.metadata.json`** — that suffix collides with the sidecar convention and the ingest
  handler would skip the file forever. **[R3]**
- `documentId = randomUUID()`; key = `uploads/<auth.sub>/<documentId>/<safeName>`.
- **Sign `ContentType` into the request** (`new PutObjectCommand({ ..., ContentType })`) so
  the browser cannot upload a different type than it declared. The client must send a
  matching `Content-Type` header on the PUT.
- **Write a Bedrock metadata sidecar** at `<key>.metadata.json` — the adjacent
  `<filename>.<ext>.metadata.json` convention. Exact shape, no elisions: **[R3]**
  ```json
  {
    "metadataAttributes": {
      "documentId":  { "value": { "type": "STRING", "stringValue": "<uuid>" },      "includeForEmbedding": false },
      "uploaderSub": { "value": { "type": "STRING", "stringValue": "<cognito sub>" }, "includeForEmbedding": false },
      "uploadedAt":  { "value": { "type": "NUMBER", "numberValue": 1730000000 },     "includeForEmbedding": false }
    }
  }
  ```
  `uploadedAt` is **epoch seconds as a NUMBER**, not a string, so Stage 2 can range-filter
  on it. Stage 1 applies no filter, but Bedrock only attaches metadata present at ingestion
  time — adding it later means re-ingesting everything. Comment that.
- Write the `DOC#` item with **`status: 'UPLOADING'`** (§4.8 state machine).
- Use `presignPut` from `lib/presigner.ts` (§4.12), not `getSignedUrl` directly.
- Return `{ documentId, uploadUrl, key }`, `expiresIn: 300`.

**`handlers/ingest.ts`** — EventBridge `Object Created` event. **[R3]**
- Read `detail.object.key`; **URL-decode it** (`+` is a space; `%20` etc.).
- **Skip any key ending in `.metadata.json`** — sidecars are not documents, and the
  sidecar PUT raises its own `Object Created` event.
- Parse `uploads/<sub>/<documentId>/<name>`; if the shape does not match, log and return.
- Verify the authoritative object size from `detail.object.size` against
  `MAX_UPLOAD_BYTES`. The presign check trusts a client-declared number; this is the real
  one. **If it exceeds: delete both the object and its sidecar from the bucket**, set
  `status: 'FAILED'` with `errorMessage`, and return. Deleting is mandatory — an oversized
  file left in the bucket sits inside the data source's scope and would be indexed later.
  **[R3]**
- **[R4] Order the write AFTER the Bedrock call, not before.** Writing `PENDING` first is not
  crash-safe: if the function dies before `IngestKnowledgeBaseDocuments`, the EventBridge
  retry fails its `expected: 'UPLOADING'` condition and the reconciler — which polls only
  `INGESTING` — ignores the row forever. Call Bedrock first, then transition
  `UPLOADING → INGESTING` in one conditional write accepting **either** `UPLOADING` or
  `PENDING` as the prior state. A failure *after* Bedrock accepted must never write `FAILED`
  (the document is really indexing); log and let the reconciler resolve it.
- Call **`IngestKnowledgeBaseDocumentsCommand`** for this one document. **[R4] The sidecar
  must be attached explicitly** — unlike a data-source *sync*, direct ingestion does not
  auto-discover `<key>.metadata.json`, so omitting `metadata` silently produces chunks with
  no `documentId` and kills every citation link:
  ```ts
  { knowledgeBaseId, dataSourceId, documents: [{
      content:  { dataSourceType: 'S3', s3: { s3Location: { uri: `s3://${bucket}/${key}` } } },
      metadata: { type: 'S3_LOCATION',
                  s3Location: { uri: `s3://${bucket}/${key}.metadata.json` } },
  }]}
  ```
  Set `status: 'INGESTING'`. On error set `FAILED` with the message.
  The ingest test **must assert the `metadata` field is present and points at the sidecar** —
  a content-only request passes an under-specified test while breaking citations end to end.
- **Never rethrow** — an unhandled throw triggers EventBridge redelivery and a loop.

**[R3] Why direct ingestion instead of `StartIngestionJob`.** `StartIngestionJob` syncs the
*entire data source*, permits only one concurrent job, and `GetIngestionJob` reports only
**aggregate** counts (`numberOfDocumentsFailed`) — it cannot tell you which file failed. That
mismatch is unfixable at the document-status level: a job can report `COMPLETE` while a
specific upload failed to parse. `IngestKnowledgeBaseDocuments` ingests one named S3 object
and `GetKnowledgeBaseDocuments` returns **per-document** status, which is exactly the model
the UI needs. It also removes the single-concurrent-job constraint, and with it the
start-race and batch-snapshot problems entirely. Grant
`bedrock:IngestKnowledgeBaseDocuments` and `bedrock:GetKnowledgeBaseDocuments`; **drop**
`StartIngestionJob`, `GetIngestionJob`, and `ListIngestionJobs` from every role.

**`handlers/reconciler.ts`** — EventBridge schedule, `rate(1 minute)`,
`reservedConcurrentExecutions: 1` so runs cannot overlap. **[R3]** Now a pure poller with
no start-side responsibility:
1. Query `gsi1pk = 'ORG#DOCUMENT'`, **paginating all pages**, for items in `INGESTING`.
   Call `GetKnowledgeBaseDocuments` in **batches of 10** (the API maximum), keyed by each
   document's `s3://` URI. Map `INDEXED → READY`; `FAILED`, `NOT_FOUND`, `IGNORED`,
   `METADATA_UPDATE_FAILED`, and any partial-index status → `FAILED` with the returned
   `statusReason` in `errorMessage`; leave `IN_PROGRESS`/`STARTING`/`PENDING` untouched.
2. Sweep abandoned uploads: any document still `UPLOADING` older than
   `ABANDONED_UPLOAD_MINUTES` (10) → `FAILED`, and delete its orphaned sidecar. This
   handles a user who requested a presigned URL and never completed the PUT.
3. All writes are conditional on the current status, so a run that overlaps the ingest
   handler cannot clobber a newer state.
4. Log one summary line of counts per run.

### [R3] Document status machine

```
UPLOADING ──(object created, size OK)──► PENDING ──(IngestKnowledgeBaseDocuments)──► INGESTING
    │                                                                                    │
    └──(10 min, no object)──► FAILED ◄──(oversize / ingest error / GetKB* terminal)──────┘
                                                    INGESTING ──(INDEXED)──► READY
```

`presign` writes **`UPLOADING`**, not `PENDING`. **[R3]** Revision 2 wrote `PENDING` before
the object existed, so a user who abandoned the browser upload left a phantom document that
could later be marked `READY` with no source object behind it.

### 4.9 [R2] `handlers/documents.ts`

- `GET /v1/documents` — `Query` on `gsi1` where `gsi1pk = 'ORG#DOCUMENT'`,
  `ScanIndexForward: false`, `Limit` from `?limit=` (default 25, max 100), optional
  `?nextToken=`. Return `{ items: DocumentSummary[], nextToken?: string }` where
  `nextToken` is `LastEvaluatedKey` JSON, base64url-encoded. Reject a malformed token
  with 400.
- `GET /v1/documents/{documentId}/download` — **new in R2**, because an `s3://` URI is
  neither browser-navigable nor authorized, and revision 1's citation chips linked to one.
  `GetItem` the doc; 404 if absent; 409 if `status !== 'READY'`; then return `{ url }` from
  **`presignGet({ bucket, key: item.s3Key, expiresIn: 300 })`** — the §4.12 wrapper, never
  `getSignedUrl` directly, so the test can mock the boundary without credential resolution.
  **[R3]** Authorization is "any authenticated user" per the org-shared decision (§0) —
  state that in a comment so it reads as deliberate rather than forgotten.

### 4.10 [R2] `handlers/sessions.ts`

- `GET /v1/sessions` — `Query` on `gsi1` where `gsi1pk = 'USER#<sub>#SESSION'`,
  newest-first, same `{ items, nextToken? }` pagination contract as documents.

**[R3] Response DTOs — define these in `shared/src/api.ts`; do not let the implementation
invent them:**

```ts
type DocumentSummary = { documentId, title, contentType, sizeBytes, status, uploadedAt };
type SessionSummary  = { sessionId, title, createdAt, updatedAt, messageCount };
type MessageView     = { role: 'user' | 'assistant', content, citations: Citation[], createdAt };
type SessionDetail   = SessionSummary & { messages: MessageView[], nextToken?: string };
type Page<T>         = { items: T[]; nextToken?: string };
```

`GET /v1/sessions/{sessionId}` returns the **latest page** of messages (default 50, max
100), newest-first in storage order but reversed to chronological in the payload, with a
`nextToken` for older pages. It must not return an unbounded message list.

**[R3] Pagination tokens must be validated, not merely decoded.** `nextToken` is
base64url-encoded `LastEvaluatedKey` JSON. On receipt, decode, then verify the key has
exactly the expected attributes, that they are strings, and that its partition **matches the
partition the caller is allowed to read** (`ORG#DOCUMENT`, or `USER#<caller sub>#SESSION`).
Reject anything else with 400 — otherwise a crafted token pages into another user's
partition.

**[R3] New-session creation, specified:** `sessionId = randomUUID()` server-side (never
taken from the client); `title` = the first 60 characters of the user's first message,
whitespace-collapsed; `createdAt`/`updatedAt` = ISO-8601; `ttl` = **epoch seconds** 90 days
out (a number, not a string — DynamoDB TTL ignores strings and the row would never expire);
`messageCount` incremented via an `ADD` update expression. The three writes (user message,
assistant message, `META` update) go in a single `TransactWriteItems` so a partial failure
cannot leave `messageCount` disagreeing with the stored messages. Creating a session uses a
`ConditionExpression: 'attribute_not_exists(pk)'`.
- `GET /v1/sessions/{sessionId}` — **`GetItem` the `META` row first and compare its
  `userSub` attribute to the caller.** Return 404 if absent, 403 on mismatch. Only after
  that check may the handler query message items. Never read or return message items
  before ownership is established.

### 4.11 `lib/logger.ts`

Dependency-free structured logger. `log(level, msg, fields)` writes one JSON line with
`level`, `msg`, `requestId`, `userSub` when known, plus extras. Every handler logs one
`info` on entry and one on completion with `durationMs`. **Never log** the JWT, a
presigned URL, or full document text.

### 4.12 [R2] `lib/presigner.ts`

`getSignedUrl` from `@aws-sdk/s3-request-presigner` runs credential resolution and
middleware — it is **not** interceptable by `aws-sdk-client-mock`. Wrap it:

```ts
export async function presignPut(opts: {...}): Promise<string>
export async function presignGet(opts: {...}): Promise<string>
```

Tests mock **this module** with `vi.mock`, not the S3 client. This is the one documented
exception to the "mock AWS with `aws-sdk-client-mock`" rule in §0.

---

## 5. Frontend

React 19 + TypeScript + Vite. **No UI framework and no CDN script tags** — the old page
pulled Tailwind from a CDN; that must not return. Plain CSS in `src/styles.css`. Clean and
readable; this is a study project, not a design exercise.

### [R2] Auth — PKCE **plus** `state` and `nonce`

PKCE protects the authorization code; it does **not** provide login-CSRF protection. On
login: generate a code verifier, a random `state`, and a random `nonce`; store all three
in `sessionStorage`; include `state` and `nonce` in the authorize URL. On `/callback`:
**validate `state` matches** before exchanging the code, exchange at `/oauth2/token`, then
**cryptographically validate the returned ID token** — signature against the pool's JWKS,
plus `iss`, `aud`, `exp`, and `nonce` — using a real JWT/OIDC library (`aws-jwt-verify`
works in the browser). **[R3] Decoding the payload and string-comparing `nonce` is not
sufficient** and must not be what gets implemented. Clear all three stored values
afterwards. Reject and return to login on any failure.

Tokens live in `sessionStorage` (not `localStorage` — less XSS persistence). Send the
**access token** as `Authorization: Bearer` to both APIs. Refresh when within 5 minutes of
expiry.

### Config

Vite env vars, all six documented in `.env.example`: `VITE_USER_POOL_ID`,
`VITE_USER_POOL_CLIENT_ID`, `VITE_COGNITO_DOMAIN`, `VITE_API_URL`, `VITE_CHAT_URL`,
`VITE_AWS_REGION`.

### Components

`LoginScreen`, `AppShell`, `DocumentPanel` (presigned PUT with progress; list with status
badges; poll `GET /v1/documents` every 10 s while any doc is `PENDING`/`INGESTING`, stop
when none are), `ChatPanel`, `SessionList`.

### [R2] SSE consumption (`api/sse.ts`)

The endpoint is `POST`, so `EventSource` cannot be used. Read `response.body.getReader()`
and:
- Decode with **`decoder.decode(chunk, { stream: true })`** and a final flush after the
  loop. Without `{ stream: true }` a multi-byte UTF-8 character split across two network
  chunks is corrupted — the sample documents and the expected answers are Chinese, so this
  is a live failure mode, not a theoretical one.
- Buffer across chunks and split frames on `\n\n`; keep any trailing partial frame in the
  buffer and never emit it until terminated.
- Parse `event:` and `data:` lines; ignore unknown event names rather than throwing.

### Citations

Replace each `[ref:N]` in assistant text with a superscript chip. Clicking a chip calls
`GET /v1/documents/{documentId}/download` and opens the returned presigned URL. Render
message text as **plain text with chips substituted** — no `dangerouslySetInnerHTML`, no
Markdown renderer in Stage 1.

---

## 6. Model IDs and region configuration

`infra/cdk.json` `context`, all overridable with `-c key=value`:

```json
{
  "frontendOrigin": "http://localhost:5173",
  "cognitoDomainPrefix": "",
  "embeddingModelId": "amazon.titan-embed-text-v2:0",
  "embeddingDimension": 1024,
  "chatModelId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
}
```

**[R2] `embeddingDimension` is read from context and threaded into `CfnIndex` — it is not
hardcoded anywhere.** Revision 1 both hardcoded `1024` and declared the value overridable.
Validate in `bin/app.ts` that when `embeddingModelId` is `amazon.titan-embed-text-v2:0`,
`embeddingDimension` is one of `1024 | 512 | 256`, and throw otherwise. A dimension that
disagrees with the embedding model fails only at ingestion time, which is miserable to debug.

`cognitoDomainPrefix: ""` means **derive** `kb-assistant-${account}` (§3.3), not "use an
empty prefix".

`chatModelId` is a **regional cross-region inference profile ID**, not a bare model ID.
Newer Claude models on Bedrock are reachable only through inference profiles and the
correct ID varies by region. The README instructs the user to confirm theirs with:

```bash
aws bedrock list-inference-profiles --region <region> \
  --query "inferenceProfileSummaries[?contains(inferenceProfileId,'anthropic')].inferenceProfileId"
```

and to enable Bedrock model access in the console before deploying.

**IAM must grant both ARNs** — Bedrock authorizes an inference-profile call against the
profile *and* the underlying foundation models, and granting only one yields a silent
`AccessDeniedException`:
- `arn:aws:bedrock:<region>:<account>:inference-profile/<chatModelId>`
- `arn:aws:bedrock:*::foundation-model/<model-id-with-the-region-prefix-stripped>`

Comment this at the policy site.

---

## 7. Tests and the definition of done

**vitest** for all TypeScript; `aws-sdk-client-mock` (+ `aws-sdk-client-mock-vitest` for
matchers) for AWS SDK clients; `vi.mock` for `lib/presigner.ts` (§4.12) and
`aws-jwt-verify`. No test may hit the network or resolve credentials.

### `backend/src/__tests__/`

1. **`agent-tool-loop.test.ts`** — mock `ConverseStreamCommand` to return a scripted async
   iterable: a `contentBlockStart` carrying `toolUse.toolUseId`, `toolUse.input` split
   across **three** fragments, `stopReason: 'tool_use'`; then a second response streaming
   text containing `[ref:1]` with `stopReason: 'end_turn'`. Assert the input parses, the
   assistant turn is reconstructed and appended before the `toolResult`, the `toolResult`
   is in a `user` message carrying the right `toolUseId`, and events emit in order
   `tool_use → citation → text`.
2. **`agent-multi-tool.test.ts` [R2]** — one assistant turn containing **two** tool-use
   blocks. Assert both run and both `toolResult`s land in a **single** user message.
3. **`agent-ref-numbering.test.ts` [R2]** — two successive retrieval calls each returning
   two chunks. Assert refs are `1,2` then `3,4` — never restarting at 1 — and that
   `[ref:3]` resolves to the second call's first chunk.
4. **`agent-tool-error.test.ts`** — `retrieve` rejects; assert a `toolResult` with
   `status: 'error'` is sent and the loop still completes rather than throwing.
5. **`agent-stream-exception.test.ts` [R2]** — the stream yields a
   `modelStreamErrorException` mid-response; assert a typed error surfaces and accumulated
   text is still returned.
6. **`agent-max-iterations.test.ts`** — model always returns `stopReason: 'tool_use'`;
   assert exactly `MAX_TOOL_ITERATIONS` iterations and `stopReason: 'max_iterations'`.
7. **`citations.test.ts`** — extraction, dedup, order; `[ref:99]` dropped by `resolveCitations`.
8. **`auth.test.ts`** — missing header, malformed header, rejected token, success.
9. **`chat-handler.test.ts` [R2]** — asserts the §4.7 ordering: an invalid token yields a
   plain `401` and **no** SSE frame is written; a valid request writes `session` first and
   `done` last; a session owned by another user yields `403` before any streaming.
10. **`presign.test.ts`** — rejects disallowed content type and oversize; sanitizes
    `../../etc/passwd` and a hostile unicode name into a safe key; rejects a name that
    sanitizes to empty; writes the `PENDING` record **and** the `.metadata.json` sidecar.
11. **`ingest.test.ts` [R3]** — URL-decodes a key with `+` and `%20`; **skips
    `.metadata.json` keys**; on oversize, **deletes both the object and the sidecar** and
    marks `FAILED`; on the happy path calls `IngestKnowledgeBaseDocuments` with the right
    `s3://` URI and moves `UPLOADING → PENDING → INGESTING`; never rethrows.
12. **`reconciler.test.ts` [R3]** — `INDEXED` → `READY`; `FAILED`/`NOT_FOUND`/`IGNORED` →
    `FAILED` with `statusReason`; `IN_PROGRESS` untouched; **batches `GetKnowledgeBaseDocuments`
    in groups of 10** and paginates the DynamoDB query past one page; sweeps an `UPLOADING`
    document older than the cutoff to `FAILED` and deletes its orphaned sidecar; conditional
    writes do not clobber a status changed concurrently.
12b. **`documents.test.ts` [R3]** — the download route mocks `lib/presigner.ts` and asserts
    no credential resolution occurs; 404 unknown, 409 when not `READY`.
13. **`sessions.test.ts` [R2]** — `GET /v1/sessions/{id}` for another user's session
    returns 403 and performs **no** message query.
14. **`ddb.test.ts`** — key shapes; `gsi1pk` is entity-typed; pagination token round-trips.

### `infra/test/stacks.test.ts`

15. Using `aws-cdk-lib/assertions` `Template`:
    - Bucket: `BlockPublicAccess` all true, KMS encryption, `NotificationConfiguration`
      has **EventBridge enabled**
    - Table: PITR on, `gsi1` present, TTL on `ttl`
    - `AWS::Bedrock::KnowledgeBase` has `StorageConfiguration.Type === 'S3_VECTORS'` and a
      non-empty `Name`
    - `AWS::Bedrock::DataSource` has a `Name` and a `KnowledgeBaseId`
    - `AWS::S3Vectors::Index` has `Dimension: 1024`, `DistanceMetric: 'cosine'`
    - Chat Function URL has `InvokeMode: 'RESPONSE_STREAM'`
    - Chat function has `ReservedConcurrentExecutions`
    - A `AWS::Events::Rule` exists with `ScheduleExpression: 'rate(1 minute)'`
    - User pool `Schema` contains an entry named **`department`** (not `custom:department`
      — the `custom:` prefix appears only in tokens)
    - **[R3]** No authored function `Environment.Variables` map contains `AWS_REGION`
    - **[R3]** Every protected `AWS::ApiGatewayV2::Route` carries
      `AuthorizationScopes: ['kb-api/access']`
    - **[R3]** With `-c embeddingDimension=512`, **both** `AWS::S3Vectors::Index.Dimension`
      **and** the KB's `BedrockEmbeddingModelConfiguration.Dimensions` are 512
    - **[R3]** No policy in any authored role grants `bedrock:StartIngestionJob`,
      `bedrock:GetIngestionJob`, or `bedrock:ListIngestionJobs`
    - **[R3]** Exact per-role action sets for presign, ingest, reconciler, and documents —
      assert the actual action lists, since the wildcard test in #16 cannot catch a
      *missing* action
16. **[R2] Narrowed wildcard assertion.** Revision 1 asserted no policy anywhere has
    `Resource: "*"`, which CDK-generated service and custom-resource policies legitimately
    violate. Instead: collect the six Lambda execution roles **we author** by logical-ID
    prefix, and assert that **their inline policy statements** contain no `Resource: "*"`,
    allowlisting only `xray:PutTraceSegments` / `xray:PutTelemetryRecords`. Do not assert
    over CDK-synthesized helper roles or the KMS key policy.

### `frontend/src/__tests__/`

17. **`sse.test.ts`** — multiple frames in one chunk; one frame split across two chunks; a
    JSON payload split mid-object; a trailing partial frame that never completes (must not
    emit); **and a multi-byte UTF-8 Chinese character split across two chunks decoding
    intact**. **[R2]**
18. **`citations.test.tsx`** — `[ref:N]` substitution renders chips and leaves surrounding
    text intact; unknown refs render as literal text.

### `evals/tests/test_dataset.py`

19. Loads `datasets/golden.json`; validates each record has `id`, `question`,
    `expected_facts` (non-empty list of str), `expected_sources` (list of str); rejects
    duplicate ids and empty questions. Ship **at least 5 real golden records** derived from
    `samples/*.md`.

### Definition of done

**[R3] Bootstrap is separate from verify.** `npm ci` does not install Python dependencies,
and `pytest` is **not currently installed on this machine** — revision 2's gate would have
failed at the last step on a clean checkout. Setup is a one-time command:

```bash
npm ci
python3 -m venv .venv
.venv/bin/python -m pip install -e './evals[test]'
```

`evals/pyproject.toml` declares `requires-python = ">=3.9"`, an optional-dependency group
`test = ["pytest>=7,<9"]`, and `[tool.pytest.ini_options] pythonpath = ["src"]` so the
`src/` layout is importable. Add `.venv/` to `.gitignore`.

Root `package.json` gets a `verify` script — it does **not** call `npm ci` itself:

```bash
npm run verify   # runs, in order:
  npm run typecheck                                # tsc --noEmit, strict, 0 errors
  npm run lint                                     # eslint, 0 errors, 0 warnings
  npm test                                         # vitest: backend + frontend + infra
  npm run -w frontend build
  npm run -w infra build && npx -w infra cdk synth # succeeds with NO AWS credentials
  .venv/bin/python -m pytest -q evals/tests        # green on Python 3.9
```

**Pin the CDK toolchain. [R3]** Pin `aws-cdk-lib` to a 2.x release that actually exports
`aws_s3vectors`, pin a matching `aws-cdk` CLI as a devDependency, and pin `esbuild` (§3.5).
`npx -w infra cdk` must resolve the local CLI and never download one — an offline synth
cannot fetch a package.

---

## 8. Conventions

- **Object arguments** for any function with more than two parameters:
  `retrieve({ query, topK })`. The old code did this consistently and it reads well.
- No relative import chains deeper than two levels.
- Short module-level docblocks explaining **why**; inline comments only for non-obvious
  decisions. Do not narrate what the next line does.
- Typed errors in `lib/errors.ts` (`UnauthorizedError`, `ForbiddenError`, `NotFoundError`,
  `ValidationError`, `ConflictError`, `BedrockStreamError`), mapped to status codes at the
  handler boundary. Keep the deliberate "tool errors go back to the model, not the client"
  behavior.
- TypeScript `strict: true`, `noUncheckedIndexedAccess: true`,
  `exactOptionalPropertyTypes: true`. No `any` — use `unknown` and narrow. No `!` outside tests.
- **English** for all code, comments, docs, UI strings, and the system prompt. The model is
  instructed to answer in the user's language, so a Chinese question still gets a Chinese answer.
- No static secrets anywhere — Cognito and IAM cover auth.

---

## 9. README

Rewrite completely, **in English**:

1. What this is; table mapping feature → AWS AI-exam domain.
2. ASCII architecture diagram (the old README's diagrams were genuinely good — match that style).
3. Repo layout.
4. Prerequisites: Node 24, Python 3.9+, AWS CLI, CDK v2, **Bedrock model access enabled**,
   and the `list-inference-profiles` command from §6.
5. Local development; `npm run verify`.
6. Deployment: `cdk bootstrap`, `cdk deploy --all`, `aws cognito-idp admin-create-user`,
   then wire the frontend `.env`.
7. **Security model**, stated plainly: documents are org-shared and readable by any
   authenticated user; conversations are private per user; the chat Function URL is public
   and verifies the token in-code, bounded by reserved concurrency.
8. **Cost note**: S3 Vectors + DynamoDB on-demand + Lambda is roughly a few dollars/month
   idle; Bedrock is per-token.
9. Cleanup: `cdk destroy --all`.
10. **Stage 2 roadmap** — Guardrails, PII masking, department/date metadata filters, hybrid
    search + reranker, Step Functions human approval, token/latency/cost dashboards,
    golden-dataset regression runner, two-model comparison — and the hooks that already
    exist for each (the `department` Cognito attribute, the metadata sidecar written at
    upload, the retrieval-filter comment, the tool loop, the `evals/` package).
11. Note that the root `.env` holds now-unused OpenAI/Voyage keys the user should rotate
    and delete.

---

## 10. Out of scope — do NOT build

Guardrails, PII detection/masking, metadata **filtering** (the sidecar is written, but no
filter is applied), hybrid search, reranking, Step Functions, human approval, cost
dashboards, the golden-dataset **runner**, model A/B comparison, WAF, custom domains,
CI/CD, multi-region, VPC/PrivateLink.

Leave only the named hook comments described above. Do not create empty files or unused
handlers for Stage 2 — a missing feature is clearer than a stub that looks implemented.
