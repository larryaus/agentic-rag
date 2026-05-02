"""Idempotently ingest sample/*.md so a fresh `docker compose up` is useful."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_service.db.pool import close_pool, get_pool  # noqa: E402
from agent_service.ingest.pipeline import ingest_bytes  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


async def main() -> None:
    pool = await get_pool()
    files = sorted(p for p in SAMPLES.glob("*.md") if p.is_file())
    if not files:
        print("[seed] no sample files found, skipping")
        return
    for f in files:
        data = f.read_bytes()
        try:
            res = await ingest_bytes(
                pool,
                data=data,
                filename=f.name,
                declared_mime="text/markdown",
                title=f.stem,
            )
            tag = "duplicated" if res["duplicated"] else "ingested"
            print(f"[seed] {tag}: {f.name} -> doc {res['document_id']} ({res['chunk_count']} chunks)")
        except Exception as exc:
            print(f"[seed] failed {f.name}: {exc}")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
