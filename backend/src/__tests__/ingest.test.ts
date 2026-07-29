import {
  BedrockAgentClient,
  IngestKnowledgeBaseDocumentsCommand,
} from '@aws-sdk/client-bedrock-agent';
import { DeleteObjectCommand, S3Client } from '@aws-sdk/client-s3';
import {
  DynamoDBDocumentClient,
  GetCommand,
  UpdateCommand,
} from '@aws-sdk/lib-dynamodb';
import type { Context } from 'aws-lambda';
import { mockClient } from 'aws-sdk-client-mock';
import { beforeEach, describe, expect, it } from 'vitest';

import { handleIngest } from '../handlers/ingest';

const bedrock = mockClient(BedrockAgentClient);
const s3 = mockClient(S3Client);
const ddb = mockClient(DynamoDBDocumentClient);
const context = { awsRequestId: 'request' } as Context;
const documentId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';

function event(
  key: string,
  size = 100,
): Parameters<typeof handleIngest>[0] {
  return {
    version: '0',
    id: 'event',
    'detail-type': 'Object Created',
    source: 'aws.s3',
    account: '123456789012',
    time: '2026-01-01T00:00:00Z',
    region: 'us-east-1',
    resources: [],
    detail: {
      bucket: { name: 'test-documents' },
      object: { key, size },
    },
  };
}

describe('ingest handler', () => {
  beforeEach(() => {
    bedrock.reset();
    s3.reset();
    ddb.reset();
    ddb.on(GetCommand).resolves({ Item: { status: 'UPLOADING' } });
    ddb.on(UpdateCommand).resolves({});
    s3.on(DeleteObjectCommand).resolves({});
    bedrock.on(IngestKnowledgeBaseDocumentsCommand).resolves({});
  });

  it('URL-decodes keys and performs direct per-document ingestion', async () => {
    await handleIngest(
      event(`uploads/user/${documentId}/My+File%20One.md`),
      context,
    );

    expect(
      bedrock.commandCalls(IngestKnowledgeBaseDocumentsCommand)[0]?.args[0]
        .input,
    ).toEqual(
      expect.objectContaining({
        knowledgeBaseId: 'test-knowledge-base',
        dataSourceId: 'test-data-source',
        documents: [
          {
            content: {
              dataSourceType: 'S3',
              s3: {
                s3Location: {
                  uri: `s3://test-documents/uploads/user/${documentId}/My File One.md`,
                },
              },
            },
            metadata: {
              type: 'S3_LOCATION',
              s3Location: {
                uri: `s3://test-documents/uploads/user/${documentId}/My File One.md.metadata.json`,
              },
            },
          },
        ],
      }),
    );
    const statuses = ddb
      .commandCalls(UpdateCommand)
      .map((call) => call.args[0].input.ExpressionAttributeValues?.[':status']);
    expect(statuses).toEqual(['INGESTING']);
    expect(
      ddb.commandCalls(UpdateCommand)[0]?.args[0].input,
    ).toEqual(
      expect.objectContaining({
        ConditionExpression: '#status IN (:expected0, :expected1)',
        ExpressionAttributeValues: expect.objectContaining({
          ':expected0': 'UPLOADING',
          ':expected1': 'PENDING',
        }),
      }),
    );
  });

  it('skips sidecar events', async () => {
    await handleIngest(
      event(`uploads/user/${documentId}/doc.md.metadata.json`),
      context,
    );
    expect(bedrock.calls()).toHaveLength(0);
    expect(ddb.calls()).toHaveLength(0);
  });

  it('deletes an oversized object and its sidecar and marks it failed', async () => {
    const key = `uploads/user/${documentId}/large.pdf`;
    await handleIngest(event(key, 26_214_401), context);

    expect(
      s3.commandCalls(DeleteObjectCommand).map((call) => call.args[0].input.Key),
    ).toEqual([key, `${key}.metadata.json`]);
    expect(
      ddb.commandCalls(UpdateCommand)[0]?.args[0].input
        .ExpressionAttributeValues,
    ).toEqual(
      expect.objectContaining({
        ':expected0': 'UPLOADING',
        ':status': 'FAILED',
      }),
    );
    expect(bedrock.calls()).toHaveLength(0);
  });

  it('leaves an ambiguous Bedrock failure for reconciliation and never rethrows', async () => {
    bedrock
      .on(IngestKnowledgeBaseDocumentsCommand)
      .rejects(new Error('parser failed'));

    await expect(
      handleIngest(
        event(`uploads/user/${documentId}/doc.md`),
        context,
      ),
    ).resolves.toBeUndefined();
    expect(ddb.commandCalls(UpdateCommand)).toHaveLength(0);
  });

  it('ignores a reused upload URL once the document has advanced', async () => {
    const key = `uploads/user/${documentId}/ready.pdf`;
    ddb.on(GetCommand).resolves({ Item: { status: 'READY' } });

    await handleIngest(event(key, 26_214_401), context);

    expect(s3.calls()).toHaveLength(0);
    expect(bedrock.calls()).toHaveLength(0);
    expect(ddb.commandCalls(UpdateCommand)).toHaveLength(0);
  });

  it('leaves oversized cleanup retryable when either deletion fails', async () => {
    const key = `uploads/user/${documentId}/large.pdf`;
    s3.on(DeleteObjectCommand, {
      Bucket: 'test-documents',
      Key: key,
    }).rejects(new Error('temporary S3 failure'));

    await handleIngest(event(key, 26_214_401), context);

    expect(s3.commandCalls(DeleteObjectCommand)).toHaveLength(2);
    expect(ddb.commandCalls(UpdateCommand)).toHaveLength(0);
  });

  it('reuses one client token and never marks accepted ingestion failed', async () => {
    ddb.on(UpdateCommand).rejects(new Error('temporary DynamoDB failure'));
    const uploadEvent = event(`uploads/user/${documentId}/doc.md`);

    await handleIngest(uploadEvent, context);
    await handleIngest(uploadEvent, context);

    const statuses = ddb
      .commandCalls(UpdateCommand)
      .map((call) => call.args[0].input.ExpressionAttributeValues?.[':status']);
    expect(statuses).toEqual([
      'INGESTING',
      'PENDING',
      'INGESTING',
      'PENDING',
    ]);
    expect(statuses).not.toContain('FAILED');
    const tokens = bedrock
      .commandCalls(IngestKnowledgeBaseDocumentsCommand)
      .map((call) => call.args[0].input.clientToken);
    expect(tokens).toEqual([
      `ingest-${documentId}`,
      `ingest-${documentId}`,
    ]);
  });
});
