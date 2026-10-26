"""Runs the golden dataset against the local deployment with real models.

    python -m kb_evals retrieval --database-url postgresql://.../postgres
    python -m kb_evals answers --database-url postgresql://.../postgres

The app reads its model settings from the environment, as in docker compose (LLM_BASE_URL,
LLM_API_KEY, LLM_MODEL, EMBEDDING_MODEL, EMBEDDING_DIMENSION...). The judge uses the same
endpoint unless JUDGE_BASE_URL, JUDGE_API_KEY or JUDGE_MODEL say otherwise. Each run uses
a fresh database on the given server and drops it afterwards. Reports are written to
--out and, in GitHub Actions, to the job summary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from kb_evals.dataset import load_dataset

DATASET = Path(__file__).resolve().parents[2] / "datasets" / "golden.json"


def publish(out: Path, name: str, markdown: str, results: object) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.md").write_text(markdown, encoding="utf-8")
    (out / f"{name}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")
    print(markdown)


async def run(args: argparse.Namespace) -> int:
    from kb_assistant.settings import Settings

    from kb_evals import harness
    from kb_evals.retrieval import load_baseline, regressions, report, score_all, summarize

    records = load_dataset(args.dataset)
    with tempfile.TemporaryDirectory() as blobs:
        async with harness.temporary_database(args.database_url) as database_url:
            settings = Settings(
                deploy_target="local",
                database_url=database_url,
                blob_root=blobs,
                oidc_issuer="http://eval.invalid",
                oidc_client_id="eval",
            )
            async with harness.local_app(settings) as (client, container):
                await harness.ingest(client, sorted(harness.SAMPLES.glob("*.md")))
                if args.command == "retrieval":
                    ranked = await harness.retrieve(container, records, settings.retrieval_top_k)
                    scores = score_all(records, ranked)
                    summary = summarize(scores)
                    baseline = load_baseline(args.baseline) if args.baseline else None
                    expected: dict[str, float] = baseline["metrics"] if baseline else {}
                    title = f"Retrieval: {settings.embedding_model}"
                    publish(
                        args.out,
                        "retrieval",
                        report(title, summary, scores, expected or None),
                        {"summary": summary, "records": [asdict(score) for score in scores]},
                    )
                    if baseline is None:
                        return 0
                    failures = regressions(summary, expected, baseline["tolerance"])
                    for failure in failures:
                        print(f"Regression: {failure}", file=sys.stderr)
                    return 1 if failures else 0

                from openai import AsyncOpenAI

                from kb_evals.answers import LlmJudge, report_answers, summarize_answers

                judge = LlmJudge(
                    AsyncOpenAI(
                        base_url=os.environ.get("JUDGE_BASE_URL") or settings.llm_base_url,
                        api_key=os.environ.get("JUDGE_API_KEY") or settings.llm_key,
                    ),
                    os.environ.get("JUDGE_MODEL") or settings.llm_model,
                )
                answered = await harness.evaluate_answers(client, records, judge)
                summary = summarize_answers(answered)
                publish(
                    args.out,
                    "answers",
                    report_answers(f"Answers: {settings.llm_model}", summary, answered),
                    {"summary": summary, "records": [asdict(score) for score in answered]},
                )
                return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m kb_evals", description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=["retrieval", "answers"])
    parser.add_argument("--database-url", required=True, help="a Postgres server with pgvector")
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--baseline", type=Path, help="fail retrieval if it falls below this")
    parser.add_argument("--out", type=Path, default=Path("eval-results"))
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
