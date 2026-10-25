import json
import time
from typing import Any

import asyncpg

from kb_assistant.core.citations import js_slice
from kb_assistant.core.paging import decode_page_token, encode_page_token
from kb_assistant.timeutil import iso_ms, now


class ConditionFailed(Exception):
    """Postgres counterpart of DynamoDB's ConditionalCheckFailedException."""


SESSION_KEYS = ["pk", "sk", "gsi1pk", "gsi1sk"]
MESSAGE_KEYS = ["pk", "sk"]


def _epoch() -> int:
    return int(time.time())


def _session(row: asyncpg.Record) -> dict[str, Any]:
    session_id = str(row["session_id"])
    return dict(
        pk=f"SESSION#{session_id}",
        sk="META",
        gsi1pk=f"USER#{row['user_sub']}#SESSION",
        gsi1sk=row["updated_at"],
        sessionId=session_id,
        userSub=row["user_sub"],
        title=row["title"],
        createdAt=row["created_at"],
        updatedAt=row["updated_at"],
        messageCount=row["message_count"],
        ttl=row["ttl"],
    )


def _message(row: asyncpg.Record) -> dict[str, Any]:
    item: dict[str, Any] = dict(
        pk=f"SESSION#{row['session_id']}",
        sk=row["sk"],
        userSub=row["user_sub"],
        role=row["role"],
        content=row["content"],
        citations=json.loads(row["citations"]),
        createdAt=row["created_at"],
        ttl=row["ttl"],
    )
    if row["usage"] is not None:
        item["usage"] = json.loads(row["usage"])
    if row["stop_reason"] is not None:
        item["stopReason"] = row["stop_reason"]
    if row["turn_id"] is not None:
        item["turnId"] = row["turn_id"]
    return item


def _document(row: asyncpg.Record) -> dict[str, Any]:
    document_id = str(row["document_id"])
    item: dict[str, Any] = dict(
        pk=f"DOC#{document_id}",
        sk="META",
        gsi1pk="ORG#DOCUMENT",
        gsi1sk=row["uploaded_at"],
        documentId=document_id,
        uploaderSub=row["uploader_sub"],
        title=row["title"],
        s3Key=row["s3_key"],
        contentType=row["content_type"],
        sizeBytes=row["size_bytes"],
        status=row["status"],
        uploadedAt=row["uploaded_at"],
        updatedAt=row["updated_at"],
    )
    if row["error_message"] is not None:
        item["errorMessage"] = row["error_message"]
    if row["recovery_attempts"] is not None:
        item["ingestionRecoveryAttempts"] = row["recovery_attempts"]
    return item


def _uuid_from(key: str, prefix: str) -> str:
    if not key.startswith(prefix):
        raise ValueError(key)
    return key[len(prefix) :]


class PgStore:
    """Same items, conditions and cursors as the DynamoDB store, backed by Postgres.

    Cursors keep DynamoDB's LastEvaluatedKey shape, so the shared routes validate them
    the same way on both deployments, and a cursor names its owner's partition.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = await self._fetchrow_by_uuid(
            "SELECT * FROM sessions WHERE session_id = $1 AND ttl > $2", session_id, _epoch()
        )
        return None if row is None else _session(row)

    async def get_document(self, document_id: str) -> dict[str, Any] | None:
        row = await self._fetchrow_by_uuid(
            "SELECT * FROM documents WHERE document_id = $1", document_id
        )
        return None if row is None else _document(row)

    async def _fetchrow_by_uuid(self, query: str, value: str, *args: Any) -> Any:
        try:
            async with self.pool.acquire() as connection:
                return await connection.fetchrow(query, value, *args)
        except asyncpg.DataError:
            # Not a UUID, so it cannot name a row; DynamoDB answers the same way.
            return None

    async def create_session(self, item: dict[str, Any]) -> None:
        try:
            async with self.pool.acquire() as connection:
                await connection.execute(
                    """INSERT INTO sessions
                       (session_id, user_sub, title, created_at, updated_at, message_count, ttl)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                    item["sessionId"],
                    item["userSub"],
                    item["title"],
                    item["createdAt"],
                    item["updatedAt"],
                    item["messageCount"],
                    item["ttl"],
                )
        except asyncpg.UniqueViolationError as exc:
            raise ConditionFailed("session exists") from exc

    async def create_document(self, item: dict[str, Any]) -> None:
        try:
            async with self.pool.acquire() as connection:
                await connection.execute(
                    """INSERT INTO documents
                       (document_id, uploader_sub, title, s3_key, content_type, size_bytes,
                        status, uploaded_at, updated_at)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                    item["documentId"],
                    item["uploaderSub"],
                    item["title"],
                    item["s3Key"],
                    item["contentType"],
                    item["sizeBytes"],
                    item["status"],
                    item["uploadedAt"],
                    item["updatedAt"],
                )
        except asyncpg.UniqueViolationError as exc:
            raise ConditionFailed("document exists") from exc

    async def persist_submitted_message(self, item: dict[str, Any]) -> None:
        await self._persist(item)

    async def persist_completed_turn(self, item: dict[str, Any]) -> None:
        await self._persist(item)

    async def _persist(self, item: dict[str, Any]) -> None:
        session_id = _uuid_from(item["pk"], "SESSION#")
        async with self.pool.acquire() as connection, connection.transaction():
            try:
                await connection.execute(
                    """INSERT INTO messages
                       (session_id, sk, user_sub, role, content, citations, usage,
                        stop_reason, turn_id, created_at, ttl)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)""",
                    session_id,
                    item["sk"],
                    item["userSub"],
                    item["role"],
                    item["content"],
                    json.dumps(item["citations"]),
                    None if "usage" not in item else json.dumps(item["usage"]),
                    item.get("stopReason"),
                    item.get("turnId"),
                    item["createdAt"],
                    item["ttl"],
                )
            except (asyncpg.UniqueViolationError, asyncpg.ForeignKeyViolationError) as exc:
                raise ConditionFailed("message rejected") from exc
            updated = await connection.execute(
                """UPDATE sessions
                   SET updated_at = $3, ttl = $4, message_count = message_count + 1
                   WHERE session_id = $1 AND user_sub = $2""",
                session_id,
                item["userSub"],
                item["createdAt"],
                item["ttl"],
            )
            if updated != "UPDATE 1":
                # Raising rolls back the message insert, like a cancelled transaction.
                raise ConditionFailed("session owner mismatch")

    async def list_sessions(self, user_sub: str, limit: int, token: str | None) -> dict[str, Any]:
        partition = f"USER#{user_sub}#SESSION"
        args: list[Any] = [user_sub, _epoch(), limit + 1]
        after = ""
        if token is not None:
            key = decode_page_token(token, SESSION_KEYS, "gsi1pk", partition)
            try:
                args += [key["gsi1sk"], _uuid_from(key["pk"], "SESSION#")]
                after = "AND (updated_at, session_id) < ($4, $5::uuid)"
            except ValueError:
                return self._malformed()
        rows = await self._fetch(
            f"""SELECT * FROM sessions WHERE user_sub = $1 AND ttl > $2 {after}
                ORDER BY updated_at DESC, session_id DESC LIMIT $3""",
            *args,
        )
        return self._page([_session(row) for row in rows], limit, SESSION_KEYS)

    async def list_documents(self, limit: int, token: str | None) -> dict[str, Any]:
        args: list[Any] = [limit + 1]
        after = ""
        if token is not None:
            key = decode_page_token(token, SESSION_KEYS, "gsi1pk", "ORG#DOCUMENT")
            try:
                args += [key["gsi1sk"], _uuid_from(key["pk"], "DOC#")]
                after = "WHERE (uploaded_at, document_id) < ($2, $3::uuid)"
            except ValueError:
                return self._malformed()
        rows = await self._fetch(
            f"""SELECT * FROM documents {after}
                ORDER BY uploaded_at DESC, document_id DESC LIMIT $1""",
            *args,
        )
        return self._page([_document(row) for row in rows], limit, SESSION_KEYS)

    async def list_messages(
        self, session_id: str, limit: int, token: str | None = None
    ) -> dict[str, Any]:
        partition = f"SESSION#{session_id}"
        args: list[Any] = [session_id, _epoch(), limit + 1]
        after = ""
        if token is not None:
            key = decode_page_token(token, MESSAGE_KEYS, "pk", partition)
            args.append(key["sk"])
            after = "AND sk < $4"
        rows = await self._fetch(
            f"""SELECT * FROM messages WHERE session_id = $1::uuid AND ttl > $2 {after}
                ORDER BY sk DESC LIMIT $3""",
            *args,
        )
        page = self._page([_message(row) for row in rows], limit, MESSAGE_KEYS)
        # Newest first from the query; callers get each page in chronological order.
        page["items"].reverse()
        return page

    def _malformed(self) -> dict[str, Any]:
        from kb_assistant.errors import ValidationError

        raise ValidationError("Malformed nextToken")

    async def _fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        try:
            async with self.pool.acquire() as connection:
                return list(await connection.fetch(query, *args))
        except asyncpg.DataError:
            return self._malformed()  # type: ignore[return-value]

    @staticmethod
    def _page(items: list[dict[str, Any]], limit: int, keys: list[str]) -> dict[str, Any]:
        page: dict[str, Any] = {"items": items[:limit]}
        if len(items) > limit:
            last = items[limit - 1]
            page["nextToken"] = encode_page_token({key: last[key] for key in keys})
        return page

    async def all_documents(self) -> list[dict[str, Any]]:
        return [
            _document(row)
            for row in await self._fetch(
                "SELECT * FROM documents ORDER BY uploaded_at DESC, document_id DESC"
            )
        ]

    async def delete_failed_document(self, document_id: str) -> bool:
        async with self.pool.acquire() as connection:
            result = await connection.execute(
                "DELETE FROM documents WHERE document_id = $1 AND status = 'FAILED'", document_id
            )
        return bool(result == "DELETE 1")

    async def transition(
        self,
        document_id: str,
        expected: list[str],
        status: str,
        error: str | None = None,
        recovery_attempt: int | None = None,
    ) -> bool:
        async with self.pool.acquire() as connection:
            result = await connection.execute(
                """UPDATE documents
                   SET status = $3, updated_at = $4, error_message = $5,
                       recovery_attempts = COALESCE($6, recovery_attempts)
                   WHERE document_id = $1 AND status = ANY($2::text[])""",
                document_id,
                expected,
                status,
                iso_ms(now()),
                None if error is None else js_slice(error, 1000),
                recovery_attempt,
            )
        return bool(result == "UPDATE 1")
