import json
from pathlib import Path

import boto3
import pytest
from botocore.httpsession import URLLib3Session
from moto import mock_aws


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_EC2_METADATA_DISABLED": "true",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    def blocked(*args, **kwargs):
        raise AssertionError("Real AWS network calls are forbidden in tests")

    monkeypatch.setattr(URLLib3Session, "send", blocked)


@pytest.fixture
def legacy():
    return lambda name: json.loads(
        (Path(__file__).parent / "fixtures" / "legacy" / f"{name}.json").read_text()
    )


@pytest.fixture
def table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        value = resource.create_table(
            TableName="test-table",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": key, "AttributeType": "S"}
                for key in ["pk", "sk", "gsi1pk", "gsi1sk"]
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "gsi1",
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
        yield value


@pytest.fixture
async def pg_url():
    """A fresh database per test on the server named by TEST_DATABASE_URL."""
    import os
    from uuid import uuid4

    import asyncpg

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    name = f"kb_test_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE DATABASE "{name}"')
    await admin.close()
    yield url.rsplit("/", 1)[0] + "/" + name
    admin = await asyncpg.connect(url)
    await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    await admin.close()
