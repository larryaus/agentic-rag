import {
  BedrockAgentClient,
  GetKnowledgeBaseDocumentsCommand,
  type KnowledgeBaseDocumentDetail,
} from '@aws-sdk/client-bedrock-agent';
import { DeleteObjectCommand, S3Client } from '@aws-sdk/client-s3';
import {
  DynamoDBDocumentClient,
  QueryCommand,
  UpdateCommand,
} from '@aws-sdk/lib-dynamodb';
import type { Context, ScheduledEvent } from 'aws-lambda';
import { mockClient } from 'aws-sdk-client-mock';
import { beforeEach, describe, expect, it } from 'vitest';

import { handleReconciler } from '../handlers/reconciler';
import type { DocumentItem } from '../lib/ddb';

const bedrock = mockClient(BedrockAgentClient);
const s3 = mockClient(S3Client);
const ddb = mockClient(DynamoDBDocumentClient);
const context = { awsRequestId: 'request' } as Context;
const schedule = {} as ScheduledEvent;

function document(index: number, status: DocumentItem['status']): DocumentItem {
  const hex = index.toString(16).padStart(12, '0');
  const documentId = `aaaaaaaa-aaaa-4aaa-8aaa-${hex}`;
  return {
    pk: `DOC#${documentId}`,
    sk: 'META',
    gsi1pk: 'ORG#DOCUMENT',
    gsi1sk: '2026-01-01T00:00:00.000Z',
    documentId,
    uploaderSub: 'user',
    title: `doc-${index}.md`,
    s3Key: `uploads/user/${documentId}/doc-${index}.md`,
    contentType: 'text/markdown',
    sizeBytes: 10,
    status,
    uploadedAt: '2026-01-01T00:00:00.000Z',
    updatedAt: '2026-01-01T00:00:00.000Z',
  };
}

function detail(
  item: DocumentItem,
  status: KnowledgeBaseDocumentDetail['status'],
  statusReason?: string,
): KnowledgeBaseDocumentDetail {
  return {
    knowledgeBaseId: 'test-knowledge-base',
    dataSourceId: 'test-data-source',
    identifier: {
      dataSourceType: 'S3',
      s3: { uri: `s3://test-documents/${item.s3Key}` },
    },
    status,
    ...(statusReason === undefined ? {} : { statusReason }),
  };
}

describe('reconciler', () => {
  beforeEach(() => {
    bedrock.reset();
    s3.reset();
    ddb.reset();
    ddb.on(UpdateCommand).resolves({});
    s3.on(DeleteObjectCommand).resolves({});
    bedrock.on(GetKnowledgeBaseDocumentsCommand).resolves({});
  });

  it('paginates, polls in groups of ten, and maps per-document statuses', async () => {
    const ingesting = Array.from({ length: 11 }, (_, index) =>
      document(index, 'INGESTING'),
    );
    const abandoned = document(99, 'UPLOADING');
    ddb
      .on(QueryCommand)
      .resolvesOnce({
        Items: ingesting.slice(0, 6),
        LastEvaluatedKey: {
          pk: ingesting[5]!.pk,
          sk: 'META',
          gsi1pk: 'ORG#DOCUMENT',
          gsi1sk: ingesting[5]!.gsi1sk,
        },
      })
      .resolvesOnce({ Items: [...ingesting.slice(6), abandoned] });
    const statuses: KnowledgeBaseDocumentDetail['status'][] = [
      'INDEXED',
      'FAILED',
      'NOT_FOUND',
      'IGNORED',
      'IN_PROGRESS',
      'INDEXED',
      'INDEXED',
      'INDEXED',
      'INDEXED',
      'INDEXED',
    ];
    bedrock
      .on(GetKnowledgeBaseDocumentsCommand)
      .resolvesOnce({
        documentDetails: ingesting
          .slice(0, 10)
          .map((item, index) =>
            detail(item, statuses[index] ?? 'IN_PROGRESS', 'status reason'),
          ),
      })
      .resolvesOnce({
        documentDetails: [
          detail(ingesting[10] as DocumentItem, 'INDEXED'),
          detail(abandoned, 'NOT_FOUND'),
        ],
      });

    await handleReconciler(schedule, context);

    expect(ddb.commandCalls(QueryCommand)).toHaveLength(2);
    const getCalls = bedrock.commandCalls(GetKnowledgeBaseDocumentsCommand);
    expect(getCalls).toHaveLength(2);
    expect(getCalls[0]?.args[0].input.documentIdentifiers).toHaveLength(10);
    expect(getCalls[1]?.args[0].input.documentIdentifiers).toHaveLength(2);
    const transitions = ddb
      .commandCalls(UpdateCommand)
      .map((call) => call.args[0].input.ExpressionAttributeValues);
    expect(
      transitions.filter((values) => values?.[':status'] === 'READY'),
    ).toHaveLength(7);
    expect(
      transitions.filter((values) => values?.[':status'] === 'FAILED'),
    ).toHaveLength(4);
    expect(
      transitions
        .filter(
          (values) =>
            values?.[':status'] === 'FAILED' &&
            values[':expected'] === 'INGESTING',
        )
        .every((values) => values?.[':reason'] === 'status reason'),
    ).toBe(true);
    expect(
      ddb
        .commandCalls(UpdateCommand)
        .some(
          (call) =>
            call.args[0].input.Key?.pk === ingesting[4]!.pk,
        ),
    ).toBe(false);
    expect(
      s3.commandCalls(DeleteObjectCommand).map((call) => call.args[0].input.Key),
    ).toEqual([abandoned.s3Key, `${abandoned.s3Key}.metadata.json`]);
  });

  it('uses a conditional status write when abandoned cleanup loses a race', async () => {
    const abandoned = document(100, 'UPLOADING');
    ddb.on(QueryCommand).resolves({ Items: [abandoned] });
    ddb.on(UpdateCommand).rejects(new Error('ConditionalCheckFailedException'));

    await handleReconciler(schedule, context);

    expect(s3.commandCalls(DeleteObjectCommand)).toHaveLength(0);
    expect(ddb.commandCalls(UpdateCommand)[0]?.args[0].input).toEqual(
      expect.objectContaining({
        ConditionExpression: '#status = :expected',
        ExpressionAttributeValues: expect.objectContaining({
          ':expected': 'UPLOADING',
          ':status': 'FAILED',
        }),
      }),
    );
  });

  it('marks an abandoned row before attempting S3 cleanup', async () => {
    const abandoned = document(101, 'UPLOADING');
    ddb.on(QueryCommand).resolves({ Items: [abandoned] });
    s3.on(DeleteObjectCommand, {
      Bucket: 'test-documents',
      Key: `${abandoned.s3Key}.metadata.json`,
    }).rejects(new Error('temporary S3 failure'));

    await handleReconciler(schedule, context);

    expect(s3.commandCalls(DeleteObjectCommand)).toHaveLength(2);
    expect(ddb.commandCalls(UpdateCommand)).toHaveLength(1);
    expect(ddb.commandCalls(UpdateCommand)[0]?.args[0].input).toEqual(
      expect.objectContaining({
        ConditionExpression: '#status = :expected',
        ExpressionAttributeValues: expect.objectContaining({
          ':expected': 'UPLOADING',
          ':status': 'FAILED',
        }),
      }),
    );
  });

  it('sweeps aged PENDING rows through per-document status polling', async () => {
    const pending = document(102, 'PENDING');
    ddb.on(QueryCommand).resolves({ Items: [pending] });
    bedrock.on(GetKnowledgeBaseDocumentsCommand).resolves({
      documentDetails: [detail(pending, 'INDEXED')],
    });

    await handleReconciler(schedule, context);

    expect(
      bedrock.commandCalls(GetKnowledgeBaseDocumentsCommand),
    ).toHaveLength(1);
    expect(ddb.commandCalls(UpdateCommand)[0]?.args[0].input).toEqual(
      expect.objectContaining({
        ConditionExpression: '#status = :expected',
        ExpressionAttributeValues: expect.objectContaining({
          ':expected': 'PENDING',
          ':status': 'READY',
        }),
      }),
    );
  });

  it('recovers an aged UPLOADING row when Bedrock already accepted it', async () => {
    const accepted = document(103, 'UPLOADING');
    ddb.on(QueryCommand).resolves({ Items: [accepted] });
    bedrock.on(GetKnowledgeBaseDocumentsCommand).resolves({
      documentDetails: [detail(accepted, 'IN_PROGRESS')],
    });

    await handleReconciler(schedule, context);

    expect(ddb.commandCalls(UpdateCommand)[0]?.args[0].input).toEqual(
      expect.objectContaining({
        ConditionExpression: '#status = :expected',
        ExpressionAttributeValues: expect.objectContaining({
          ':expected': 'UPLOADING',
          ':status': 'INGESTING',
        }),
      }),
    );
    expect(s3.commandCalls(DeleteObjectCommand)).toHaveLength(0);
  });
});
