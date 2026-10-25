import asyncio
from typing import Any, cast

import boto3
from botocore.exceptions import ClientError

from kb_assistant.core.citations import js_slice
from kb_assistant.core.paging import decode_page_token, encode_page_token
from kb_assistant.timeutil import iso_ms, now

from .ddb_codec import from_ddb, to_ddb


class DdbStore:
    def __init__(self, table_name: str, *, table: Any = None) -> None:
        self.table_name = table_name
        self.table = table if table is not None else boto3.resource("dynamodb").Table(table_name)
        # Resource instances are not thread safe; each request leaves the event loop free.
        self._lock = asyncio.Lock()

    async def _call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        async with self._lock:
            result = await asyncio.to_thread(getattr(self.table, operation), **to_ddb(kwargs))
        return cast(dict[str, Any], from_ddb(result))

    async def _get(self, pk: str) -> dict[str, Any] | None:
        result = await self._call("get_item", Key={"pk": pk, "sk": "META"}, ConsistentRead=True)
        return cast(dict[str, Any] | None, result.get("Item"))

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        return await self._get(f"SESSION#{session_id}")

    async def get_document(self, document_id: str) -> dict[str, Any] | None:
        return await self._get(f"DOC#{document_id}")

    async def create_session(self, item: dict[str, Any]) -> None:
        await self._call("put_item", Item=item, ConditionExpression="attribute_not_exists(pk)")

    async def create_document(self, item: dict[str, Any]) -> None:
        await self.create_session(item)

    async def persist_submitted_message(self, item: dict[str, Any]) -> None:
        await self._persist(item)

    async def persist_completed_turn(self, item: dict[str, Any]) -> None:
        await self._persist(item)

    async def _persist(self, item: dict[str, Any]) -> None:
        transaction = [
            {
                "Put": {
                    "TableName": self.table_name,
                    "Item": item,
                    "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)",
                }
            },
            {
                "Update": {
                    "TableName": self.table_name,
                    "Key": {"pk": item["pk"], "sk": "META"},
                    "UpdateExpression": "SET #updatedAt = :updatedAt, #gsi1sk = :gsi1sk, #ttl = :ttl ADD messageCount :one",
                    "ConditionExpression": "#userSub = :userSub",
                    "ExpressionAttributeNames": {
                        "#updatedAt": "updatedAt",
                        "#gsi1sk": "gsi1sk",
                        "#ttl": "ttl",
                        "#userSub": "userSub",
                    },
                    "ExpressionAttributeValues": {
                        ":updatedAt": item["createdAt"],
                        ":gsi1sk": item["createdAt"],
                        ":ttl": item["ttl"],
                        ":one": 1,
                        ":userSub": item["userSub"],
                    },
                }
            },
        ]
        async with self._lock:
            await asyncio.to_thread(
                self.table.meta.client.transact_write_items, TransactItems=to_ddb(transaction)
            )

    async def _page(
        self, partition: str, limit: int, token: str | None, *, messages: bool = False
    ) -> dict[str, Any]:
        keys = ["pk", "sk"] if messages else ["pk", "sk", "gsi1pk", "gsi1sk"]
        partition_key = "pk" if messages else "gsi1pk"
        kwargs: dict[str, Any] = dict(ScanIndexForward=False, Limit=limit)
        if messages:
            kwargs.update(
                KeyConditionExpression="pk = :pk AND begins_with(sk, :message)",
                ExpressionAttributeValues={":pk": partition, ":message": "MSG#"},
            )
        else:
            kwargs.update(
                IndexName="gsi1",
                KeyConditionExpression="gsi1pk = :partition",
                ExpressionAttributeValues={":partition": partition},
            )
        if token is not None:
            kwargs["ExclusiveStartKey"] = decode_page_token(token, keys, partition_key, partition)
        output = await self._call("query", **kwargs)
        items = output.get("Items", [])
        page = {"items": list(reversed(items)) if messages else items}
        if "LastEvaluatedKey" in output:
            page["nextToken"] = encode_page_token(output["LastEvaluatedKey"])
        return page

    async def list_sessions(self, user_sub: str, limit: int, token: str | None) -> dict[str, Any]:
        return await self._page(f"USER#{user_sub}#SESSION", limit, token)

    async def list_messages(
        self, session_id: str, limit: int, token: str | None = None
    ) -> dict[str, Any]:
        return await self._page(f"SESSION#{session_id}", limit, token, messages=True)

    async def list_documents(self, limit: int, token: str | None) -> dict[str, Any]:
        return await self._page("ORG#DOCUMENT", limit, token)

    async def all_documents(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = dict(
            IndexName="gsi1",
            KeyConditionExpression="gsi1pk = :partition",
            ExpressionAttributeValues={":partition": "ORG#DOCUMENT"},
        )
        while True:
            output = await self._call("query", **kwargs)
            items.extend(output.get("Items", []))
            if "LastEvaluatedKey" not in output:
                return items
            kwargs["ExclusiveStartKey"] = output["LastEvaluatedKey"]

    async def delete_failed_document(self, document_id: str) -> bool:
        try:
            await self._call(
                "delete_item",
                Key={"pk": f"DOC#{document_id}", "sk": "META"},
                ConditionExpression="#status = :failed",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":failed": "FAILED"},
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    async def transition(
        self,
        document_id: str,
        expected: list[str],
        status: str,
        error: str | None = None,
        recovery_attempt: int | None = None,
    ) -> bool:
        tokens = [f":expected{i}" for i in range(len(expected))]
        values: dict[str, Any] = {
            ":status": status,
            ":updatedAt": iso_ms(now()),
            **dict(zip(tokens, expected, strict=True)),
        }
        assignments = ["#status = :status", "updatedAt = :updatedAt"]
        if error is not None:
            assignments.append("errorMessage = :error")
            values[":error"] = js_slice(error, 1000)
        if recovery_attempt is not None:
            assignments.append("ingestionRecoveryAttempts = :attempt")
            values[":attempt"] = recovery_attempt
        condition = (
            f"#status = {tokens[0]}" if len(tokens) == 1 else f"#status IN ({', '.join(tokens)})"
        )
        try:
            await self._call(
                "update_item",
                Key={"pk": f"DOC#{document_id}", "sk": "META"},
                UpdateExpression="SET "
                + ", ".join(assignments)
                + (" REMOVE errorMessage" if error is None else ""),
                ConditionExpression=condition,
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=values,
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
