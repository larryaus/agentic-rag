# Design — Prove Stage 1 Runs on Real AWS

**Date:** 2026-07-30
**Status:** Approved, ready for implementation planning
**Predecessor:** `PLAN.md` (Stage 1 build, revision 4) · PR #6

---

## 1. Context

Stage 1 is code-complete and its verification gate is green: `tsc --noEmit` under
`strict` + `noUncheckedIndexedAccess` + `exactOptionalPropertyTypes`, eslint clean,
62 vitest tests across 21 files, `cdk synth` offline with credentials stripped, and
3 pytest tests on Python 3.9.13.

**None of it has ever run against AWS.** `cdk synth` proves the CloudFormation
templates are syntactically valid. It does not prove that Bedrock accepts the
knowledge-base configuration, that S3 Vectors indexes a document, that the Cognito
scope actually rejects an ID token, or that SSE survives a real Lambda Function URL.

Every Stage 2 feature would sit on top of that unverified foundation. This spec
closes the gap before any new functionality is added.

## 2. Goal and non-goals

**Goal.** Deploy once to a real account, verify each behaviour Stage 1 claims,
tear the stack down, and land the non-deploy hardening that should have existed
from the start.

**Deployment posture.** Deploy → verify → `cdk destroy`. Not a long-lived
environment. Expected cost: single-digit US dollars, dominated by Bedrock tokens.

**Execution.** The repository owner runs every AWS command. This spec produces a
runbook, not an automated deploy. No agent invokes `cdk bootstrap`, `cdk deploy`,
or `cdk destroy`.

**Out of scope.** All Stage 2 features (Guardrails, PII masking, metadata filters,
hybrid search, reranking, Step Functions approval, cost dashboards, the eval
runner, model A/B comparison). Also out: multi-region, custom domains, WAF,
long-lived environments.

## 3. Verified environment facts

Established by direct query on 2026-07-30, not assumed:

| Fact | Value |
|---|---|
| Account | `460377240953`, IAM user `Larry` |
| Default region | `ap-southeast-2` (Sydney) |
| S3 Vectors in region | Available — endpoint `s3vectors.ap-southeast-2.api.aws` |
| Bedrock Knowledge Bases in region | Supported |
| Embedding model in region | `amazon.titan-embed-text-v2:0` — confirmed present |
| Local `aws` CLI | **2.0.24 (2020)** — does not recognise the `bedrock` or `s3vectors` subcommands |

**Inference profile prefixes in `ap-southeast-2` are split by model age.** Older
models carry `apac.`; the current generation carries `au.`. Confirmed present:

```
apac.anthropic.claude-3-5-sonnet-20241022-v2:0    # older generation
au.anthropic.claude-sonnet-5                      # target
au.anthropic.claude-opus-5
global.anthropic.claude-sonnet-5                  # cross-region alternative
```

`PLAN.md` defaults `chatModelId` to `us.anthropic.claude-sonnet-4-5-20250929-v1:0`,
which does not exist in this region. Left unchanged, every chat request fails with
`AccessDeniedException`.

## 4. Pre-deploy corrections

Required. Each either breaks the deploy or makes a checklist item unverifiable.

### 4.1 Region-correct model default

Change the `chatModelId` context default in `infra/cdk.json` to
`au.anthropic.claude-sonnet-5`. Sonnet 5 is the cost/quality fit for a study
project; `au.anthropic.claude-opus-5` is the substitution if higher capability is
wanted later.

Keep `embeddingModelId` at `amazon.titan-embed-text-v2:0` — verified available.

The IAM grant must still cover both the inference-profile ARN and the underlying
foundation-model ARNs, as `PLAN.md` §6 requires. The grant derives the
foundation-model ARN by stripping the region prefix from `chatModelId`, so
changing `us.` → `au.` must still yield `anthropic.claude-sonnet-5`. Add an infra
test asserting that, with `-c chatModelId=au.anthropic.claude-sonnet-5`, the
synthesized chat policy contains **both**
`…:inference-profile/au.anthropic.claude-sonnet-5` and
`…::foundation-model/anthropic.claude-sonnet-5`. A prefix-stripping bug here
presents as a silent `AccessDeniedException` that looks like application code.

### 4.2 A model-ID probe that works on this machine

The README instructs the reader to run `aws bedrock list-inference-profiles`.
That command does not exist on a 2.0.24 CLI, so the runbook cannot depend on it.

Add `tools/probe-bedrock.mjs` — a read-only script using `@aws-sdk/client-bedrock`
that prints the Anthropic inference profiles and Titan embedding models available
in a given region. This approach is already proven to work against this account.
`@aws-sdk/client-bedrock` becomes a devDependency of `infra` (control-plane only;
no runtime Lambda imports it).

Update the README to offer both paths: upgrade the CLI, or run the probe.

### 4.3 Frontend origin must match the served port

`frontendOrigin` defaults to `http://localhost:5173`. Vite silently moves to 5174
when 5173 is occupied — which happened during local testing — and the Cognito
callback URL then does not match. The runbook must pass
`-c frontendOrigin=http://localhost:<actual-port>` explicitly rather than relying
on the default.

### 4.4 Guard the dev-only auth branch

`frontend/src/auth/auth.ts` carries a local-mock path gated on
`VITE_MOCK_JWKS_URI`. It verifies signature, issuer, audience, and nonce, so it is
not a bypass — but a deployed build that set the variable would trust the wrong
JWKS.

Add a CI assertion that the production `frontend/dist` bundle contains no
occurrence of `VITE_MOCK_JWKS_URI` or a `localhost` JWKS URL.

## 5. Acceptance checklist

The core deliverable. Every item is something the mocked test suite structurally
cannot prove. Each is recorded pass/fail with evidence — a screenshot, a CLI
response, or a log excerpt.

| # | Claim under test | Pass criterion |
|---|---|---|
| 1 | The `kb-api/access` scope makes the HTTP API access-token-only | A request bearing an **ID** token returns **401**; the same request with an access token returns 200 |
| 2 | Conversations are private per user | With two Cognito users, user A requesting user B's session gets **403**, and CloudWatch shows no message query was executed |
| 3 | The document state machine reaches a terminal state | `samples/company_handbook.md` progresses `UPLOADING → INGESTING → READY` under real Bedrock ingestion latency, driven by the 1-minute reconciler |
| 4 | Oversized uploads are removed, not just marked failed | A >25 MiB upload ends `FAILED`, and neither the object nor its `.metadata.json` sidecar remains in the bucket |
| 5 | Citations resolve end to end | Asking a question yields `[ref:N]` chips; clicking one downloads the correct source file via presigned GET |
| 6 | Upload itself succeeds | The browser PUT returns 200, **not** `BadDigest` — the real test of the empty-body-checksum fix |
| 7 | The response genuinely streams | In devtools, the `text/event-stream` response shows `session` and the first `text` frame arriving while the request is still pending — at least 1 s before it completes. A buffered response shows every frame at once on completion |
| 8 | Tracing is wired | A chat request produces one X-Ray trace spanning Lambda → Bedrock → DynamoDB |

Items 1, 2, 5, and 6 each correspond to a defect found during review. They are on
this list because a green unit test already exists for each and did not prove the
behaviour against the real services.

Failures are fixed in place and re-verified before teardown.

## 6. Runbook

`docs/runbooks/deploy-verify-teardown.md`, four stages. Every step states the exact
command, the expected output, and what to do when it differs.

1. **Prepare and deploy** — confirm Bedrock model access is enabled in the console,
   run the probe, `cdk bootstrap`, `cdk deploy --all` with explicit context.
2. **Seed** — create two Cognito users with `admin-create-user`, write the stack
   outputs into `frontend/.env` (and remove `VITE_MOCK_JWKS_URI`), start the frontend.
3. **Verify** — walk the §5 checklist, recording evidence per item.
4. **Tear down** — `cdk destroy --all`, then explicitly confirm no billable
   remnants: S3 Vectors bucket and index, the KMS key, CloudWatch log groups, and
   the Cognito user pool.

Stage 4 is called out because `RemovalPolicy.DESTROY` covers the resources the CDK
manages, and a leftover S3 Vectors index is the one most likely to keep costing
money unnoticed.

## 7. Non-deploy hardening

Independent of the account; can proceed regardless of when the deploy happens.

**CI.** A GitHub Actions workflow on pull requests: `npm ci`, then the existing
`verify` gate (typecheck, lint, tests, frontend build, offline `cdk synth`), plus
the Python step and the §4.4 bundle assertion. No deploy step — CI never touches
AWS, so no OIDC role is needed.

**cdk-nag.** Apply the `AwsSolutions` rule pack to every stack. Each finding is
either fixed or suppressed with a written justification. Suppressions are
reviewed, not bulk-applied — the point is to surface what was missed, and the
expectation is that some findings are real.

**Budget alarm.** An `AWS::Budgets::Budget` with an SNS notification at a $20
monthly threshold. Deliberately a backstop, not a forecast. It exists in the
template for Stage 2's longer-lived deployments even though this deployment is
destroyed.

**Cold review of the auth branch.** Send `frontend/src/auth/auth.ts` through an
independent review pass on its own. It is the one piece of security-relevant code
in the repository that has never been reviewed by anything but its author.

## 8. Sequencing

Two independent tracks. §7 needs no AWS account and can land first, in its own
pull request; §4 → §6 gates on the owner being ready to spend money.

```
§4 pre-deploy fixes ──► §6 runbook ──► §5 verification ──► teardown
§7 hardening (CI · cdk-nag · budget · auth review)  ── parallel, no account needed
```

The one ordering constraint: §4.1 and §4.2 must be complete before the runbook is
written, because the runbook's commands quote the corrected model ID and the probe
script.

## 9. Definition of done

1. All eight §5 checklist items pass, with recorded evidence.
2. `cdk destroy --all` completes and a remnant sweep confirms no billable resources.
3. CI runs green on PR #6.
4. cdk-nag reports no unaddressed findings.
5. `npm run verify` still passes locally.
6. The cold review of `auth.ts` is complete and its findings are resolved or
   consciously accepted.

## 10. Risks

**Bedrock model access may not be enabled.** Listing inference profiles proves
they exist, not that the account may invoke them. This surfaces only on the first
chat request. The runbook checks console access before deploying.

**The `au.` prefix change may break the IAM grant.** Covered by §4.1, and it is
the failure most likely to look like a code bug when it is a policy mismatch.

**S3 Vectors is young.** It went GA in December 2025 and reached this region in
March 2026. Behaviour under a real ingestion may differ from the documented
contract; that is part of what this exercise is for.

**cdk-nag will likely find real problems.** Least privilege was a Stage 1
requirement and was reviewed, but a dedicated rule pack looks at things human
review did not. Findings should be expected rather than treated as a surprise.
