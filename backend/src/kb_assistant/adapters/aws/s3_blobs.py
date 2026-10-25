import asyncio
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


class S3Blobs:
    def __init__(self, bucket: str, *, client: Any = None) -> None:
        self.bucket = bucket
        self.client: Any = (
            client
            if client is not None
            else boto3.client(
                "s3",
                config=Config(
                    signature_version="s3v4", request_checksum_calculation="when_required"
                ),
            )
        )

    async def presign_put(self, key: str, content_type: str, expires_in: int = 300) -> str:
        return str(
            await asyncio.to_thread(
                self.client.generate_presigned_url,
                "put_object",
                Params={
                    "Bucket": self.bucket,
                    "Key": key,
                    "ContentType": content_type,
                    "IfNoneMatch": "*",
                },
                ExpiresIn=expires_in,
            )
        )

    async def presign_get(self, key: str, expires_in: int = 300) -> str:
        return str(
            await asyncio.to_thread(
                self.client.generate_presigned_url,
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        )

    async def put_sidecar(self, key: str, body: str) -> None:
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=f"{key}.metadata.json",
            ContentType="application/json",
            Body=body,
        )

    async def head(self, key: str) -> int | None:
        try:
            result = await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise
        if "ContentLength" not in result:
            raise RuntimeError("S3 omitted the uploaded object size")
        return int(result["ContentLength"])

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)
