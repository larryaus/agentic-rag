from __future__ import annotations

import pytest

from kb_evals.harness import database_url


@pytest.mark.parametrize(
    ("server", "expected"),
    [
        ("postgresql://u:p@localhost:5432/postgres", "postgresql://u:p@localhost:5432/kb"),
        ("postgresql://u@db/postgres?sslmode=require", "postgresql://u@db/kb?sslmode=require"),
        (
            "postgresql://postgres@/postgres?host=/var/run/postgresql",
            "postgresql://postgres@/kb?host=/var/run/postgresql",
        ),
    ],
)
def test_database_url_replaces_only_the_database_name(server: str, expected: str) -> None:
    assert database_url(server, "kb") == expected
