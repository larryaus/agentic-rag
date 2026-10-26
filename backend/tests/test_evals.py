"""The golden-dataset evaluations, run against the local pipeline on every PR.

Embeddings and answers come from the fake model server: embeddings are hashed bags of
words, so the retrieval gate checks the pipeline deterministically (extraction,
chunking, ingestion, hybrid search and ranking), not how good a real embedding model is.
`python -m kb_evals` measures that with real models. After an intended retrieval change,
rerun with UPDATE_EVAL_BASELINE=1 and commit the new baseline.
"""

import json
import os
from pathlib import Path

import fake_llm_server
import httpx
import pytest
from kb_evals import harness
from kb_evals.answers import LlmJudge, summarize_answers
from kb_evals.dataset import load_dataset
from kb_evals.retrieval import load_baseline, regressions, report, score_all, summarize
from openai import AsyncOpenAI

from kb_assistant.settings import Settings

pytestmark = pytest.mark.postgres
EVALS = Path(__file__).parents[2] / "evals"
BASELINE = EVALS / "baselines" / "retrieval-pipeline.json"
RECORDS = load_dataset(EVALS / "datasets" / "golden.json")


def fake_model_server():
    return AsyncOpenAI(
        api_key="fake",
        base_url="http://fake-llm/v1",
        http_client=httpx.AsyncClient(transport=httpx.ASGITransport(fake_llm_server.app)),
    )


@pytest.fixture
async def ingested(pg_url, tmp_path):
    settings = Settings(
        deploy_target="local",
        database_url=pg_url,
        blob_root=str(tmp_path),
        llm_api_key="fake",
        llm_base_url="http://fake-llm/v1",
        llm_model="fake",
        embedding_model="fake-embedding",
        embedding_dimension=fake_llm_server.DIMENSION,
        oidc_issuer="http://eval.invalid",
        oidc_client_id="eval",
    )
    async with harness.local_app(settings, openai_client=fake_model_server()) as (client, container):
        await harness.ingest(client, sorted(harness.SAMPLES.glob("*.md")))
        yield client, container, settings


async def test_retrieval_does_not_fall_below_the_baseline(ingested):
    _, container, settings = ingested
    ranked = await harness.retrieve(container, RECORDS, settings.retrieval_top_k)
    scores = score_all(RECORDS, ranked)
    summary = summarize(scores)
    baseline = load_baseline(BASELINE)
    if os.environ.get("UPDATE_EVAL_BASELINE"):
        baseline = {**baseline, "metrics": summary}
        BASELINE.write_text(json.dumps(baseline, indent=2) + "\n")
    text = report("Retrieval pipeline (fake embeddings)", summary, scores, baseline["metrics"])
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    failures = regressions(summary, baseline["metrics"], baseline["tolerance"])
    assert not failures, "\n".join([*failures, "", text])


async def test_answer_evaluation_runs_turns_and_scores_them(ingested):
    client, _, _ = ingested
    records = [r for r in RECORDS if r.id in {"handbook-expenses", "support-p0", "oos-parental-leave"}]
    scores = await harness.evaluate_answers(client, records, LlmJudge(fake_model_server(), "fake"))
    by_id = {score.id: score for score in scores}
    assert all(score.stop_reason == "end_turn" for score in scores)
    # The fake model answers from the top search result and cites it.
    samples = {path.name for path in harness.SAMPLES.glob("*.md")}
    assert all(score.cited and set(score.cited) <= samples for score in scores)
    assert by_id["support-p0"].cites_expected
    # It never declines, so the uncovered question counts as not refused.
    assert not by_id["oos-parental-leave"].refused
    summary = summarize_answers(scores)
    assert summary["errors"] == 0 and summary["refusal"] == 0.0
