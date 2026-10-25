from copy import deepcopy
from decimal import Decimal

import pytest
from botocore.exceptions import ClientError

from kb_assistant.adapters.aws.ddb_codec import from_ddb, to_ddb
from kb_assistant.adapters.aws.ddb_store import DdbStore
from kb_assistant.core.history import make_message_item, make_session_item, to_history


def test_codec_omits_none_and_converts_numbers():
    value = {"a": 0.875, "b": 1.0, "c": None, "d": [{"a": None, "b": True}]}
    encoded = to_ddb(value)
    assert encoded == {"a": Decimal("0.875"), "b": Decimal("1.0"), "d": [{"b": True}]}
    result = from_ddb(encoded)
    assert type(result["a"]) is float and type(result["b"]) is int


async def test_legacy_items_equal_python_writes(table, legacy):
    store = DdbStore("test-table", table=table)
    data = legacy("items")
    session = make_session_item(**data["sessionInput"])
    assert session == data["session"]
    await store.create_session(session)
    for pair in data["messages"]:
        item = make_message_item(**pair["input"])
        assert item == pair["item"]
        table.put_item(Item=to_ddb(item))
    assert await store.get_session(data["session"]["sessionId"]) == session
    page = await store.list_messages(session["sessionId"], 50)
    assert len(page["items"]) == 3
    assert to_history(page["items"])
    assert type((await store.get_session(session["sessionId"]))["ttl"]) is int
    with pytest.raises(ClientError):
        await store.create_session(session)


async def test_legacy_float_score_survives_decimal_storage(table, legacy):
    item = deepcopy(legacy("items")["messages"][1]["item"])
    item["citations"][0]["score"] = 0.30000000000000004
    encoded = to_ddb(item)
    assert encoded["citations"][0]["score"] == Decimal("0.30000000000000004")
    table.put_item(Item=encoded)
    rows = await DdbStore("test-table", table=table).list_messages(
        item["pk"].removeprefix("SESSION#"), 50
    )
    assert rows["items"] == [item]
    assert type(rows["items"][0]["citations"][0]["score"]) is float


async def test_transactions_count_messages_and_roll_back(table, legacy):
    store = DdbStore("test-table", table=table)
    data = legacy("items")
    await store.create_session(data["session"])
    user, assistant = [p["item"] for p in data["messages"][:2]]
    await store.persist_submitted_message(user)
    meta = await store.get_session(data["session"]["sessionId"])
    assert meta["messageCount"] == 1 and meta["updatedAt"] == user["createdAt"]
    await store.persist_completed_turn(assistant)
    meta = await store.get_session(meta["sessionId"])
    assert meta["messageCount"] == 2 and meta["gsi1sk"] == assistant["createdAt"]
    with pytest.raises(ClientError):
        await store.persist_completed_turn(assistant)
    wrong = dict(user, sk=user["sk"] + "-wrong", userSub="other")
    with pytest.raises(ClientError):
        await store.persist_submitted_message(wrong)
    assert (await store.get_session(meta["sessionId"]))["messageCount"] == 2
    assert len((await store.list_messages(meta["sessionId"], 50))["items"]) == 2


async def test_legacy_cursor_paging_and_history(table, legacy):
    store = DdbStore("test-table", table=table)
    data = legacy("items")
    table.put_item(Item=to_ddb(data["session"]))
    for pair in data["messages"]:
        table.put_item(Item=to_ddb(pair["item"]))
    doc = legacy("document")["document"]
    table.put_item(Item=to_ddb(doc))
    assert (await store.list_sessions("owner", 25, None))["items"] == [data["session"]]
    assert (await store.list_documents(25, None))["items"] == [doc]
    assert await store.get_document(doc["documentId"]) == doc
    # Exact old token bytes are accepted at each partition, including keys no longer present.
    tokens = legacy("paging")
    assert (await store.list_sessions("owner", 25, tokens[0]["token"]))["items"] == []
    assert isinstance(
        (await store.list_messages(data["session"]["sessionId"], 50, tokens[1]["token"]))["items"],
        list,
    )
    assert isinstance((await store.list_documents(25, tokens[2]["token"]))["items"], list)
    page = await store.list_messages(data["session"]["sessionId"], 1)
    older = await store.list_messages(data["session"]["sessionId"], 50, page["nextToken"])
    assert len(page["items"]) + len(older["items"]) == 3


async def test_document_conditions_and_pagination(table, legacy):
    store = DdbStore("test-table", table=table)
    doc = deepcopy(legacy("document")["document"])
    await store.create_document(doc)
    with pytest.raises(ClientError):
        await store.create_document(doc)
    assert not await store.delete_failed_document(doc["documentId"])
    assert not await store.transition(doc["documentId"], ["INGESTING"], "READY")
    assert await store.transition(
        doc["documentId"], ["UPLOADING", "PENDING"], "FAILED", "x" * 1100, 2
    )
    updated = await store.get_document(doc["documentId"])
    assert updated["errorMessage"] == "x" * 1000 and updated["ingestionRecoveryAttempts"] == 2
    assert await store.transition(doc["documentId"], ["FAILED"], "INGESTING")
    assert "errorMessage" not in await store.get_document(doc["documentId"])
    assert await store.all_documents() == [await store.get_document(doc["documentId"])]
    await store.transition(doc["documentId"], ["INGESTING"], "FAILED")
    assert await store.delete_failed_document(doc["documentId"])
    assert await store.get_document(doc["documentId"]) is None


async def test_all_documents_reads_every_page(table, monkeypatch):
    calls = []
    outputs = [
        {
            "Items": [{"documentId": "one"}],
            "LastEvaluatedKey": {
                "pk": "DOC#one",
                "sk": "META",
                "gsi1pk": "ORG#DOCUMENT",
                "gsi1sk": "time",
            },
        },
        {"Items": [{"documentId": "two"}]},
    ]

    def query(**kwargs):
        calls.append(kwargs)
        return outputs.pop(0)

    monkeypatch.setattr(table, "query", query)
    assert await DdbStore("test-table", table=table).all_documents() == [
        {"documentId": "one"},
        {"documentId": "two"},
    ]
    assert len(calls) == 2 and calls[1]["ExclusiveStartKey"]["pk"] == "DOC#one"
    assert calls[0]["IndexName"] == "gsi1"
