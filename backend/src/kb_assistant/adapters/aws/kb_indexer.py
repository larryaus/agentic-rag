import asyncio
from typing import Any, cast

import boto3


class KbIndexer:
    def __init__(
        self, knowledge_base_id: str, data_source_id: str, bucket: str, *, client: Any = None
    ) -> None:
        self.knowledge_base_id = knowledge_base_id
        self.data_source_id = data_source_id
        self.bucket = bucket
        self.client: Any = client if client is not None else boto3.client("bedrock-agent")

    async def ingest(self, document_id: str, key: str) -> None:
        uri = f"s3://{self.bucket}/{key}"
        await asyncio.to_thread(
            self.client.ingest_knowledge_base_documents,
            knowledgeBaseId=self.knowledge_base_id,
            dataSourceId=self.data_source_id,
            clientToken=f"ingest-{document_id}",
            documents=[
                {
                    "content": {"dataSourceType": "S3", "s3": {"s3Location": {"uri": uri}}},
                    "metadata": {
                        "type": "S3_LOCATION",
                        "s3Location": {"uri": f"{uri}.metadata.json"},
                    },
                }
            ],
        )

    async def details(self, keys: list[str]) -> list[dict[str, Any]]:
        response = await asyncio.to_thread(
            self.client.get_knowledge_base_documents,
            knowledgeBaseId=self.knowledge_base_id,
            dataSourceId=self.data_source_id,
            documentIdentifiers=[
                {"dataSourceType": "S3", "s3": {"uri": f"s3://{self.bucket}/{key}"}} for key in keys
            ],
        )
        return cast(list[dict[str, Any]], response.get("documentDetails", []))
