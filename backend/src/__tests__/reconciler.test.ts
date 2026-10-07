import {
  BedrockAgentClient,
  GetKnowledgeBaseDocumentsCommand,
  IngestKnowledgeBaseDocumentsCommand,
  type KnowledgeBaseDocumentDetail,
} from '@aws-sdk/client-bedrock-agent';
import { ConditionalCheckFailedException } from '@aws-sdk/client-dynamodb';
import {
  DeleteObjectCommand,
  HeadObjectCommand,
  S3Client,
} from '@aws-sdk/client-s3';
import {
  DynamoDBDocumentClient,
  QueryCommand,
  UpdateCommand,
} from '@aws-sdk/lib-dynamodb';
import type { Context, ScheduledEvent } from 'aws-lambda';
import { mockClient } from 'aws-sdk-client-mock';
import { beforeEach, describe, expect, it, vi } from 'vitest';

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
    s3.on(HeadObjectCommand).rejects({
      name: 'NotFound',
      $metadata: { httpStatusCode: 404 },
    });
    bedrock.on(GetKnowledgeBaseDocumentsCommand).resolves({});
    bedrock.on(IngestKnowledgeBaseDocumentsCommand).resolves({});
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
        .some((call) => call.args[0].input.Key?.pk === ingesting[4]!.pk),
    ).toBe(false);
    expect(
      s3.commandCalls(DeleteObjectCommand).map((call) => call.args[0].input.Key),
    ).toEqual([abandoned.s3Key, `${abandoned.s3Key}.metadata.json`]);
  });

  it('uses a conditional status write when abandoned cleanup loses a race', async () => {
    const abandoned = document(100, 'UPLOADING');
    ddb.on(QueryCommand).resolves({ Items: [abandoned] });
    bedrock
      .on(GetKnowledgeBaseDocumentsCommand)
      .resolves({ documentDetails: [detail(abandoned, 'NOT_FOUND')] });
    ddb
      .on(UpdateCommand)
      .rejects(
        new ConditionalCheckFailedException({
          message: 'changed',
          $metadata: {},
        }),
      );

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
    bedrock
      .on(GetKnowledgeBaseDocumentsCommand)
      .resolves({ documentDetails: [detail(abandoned, 'NOT_FOUND')] });
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

    expect(bedrock.commandCalls(GetKnowledgeBaseDocumentsCommand)).toHaveLength(
      1,
    );
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

  it.each(['UPLOADING', 'PENDING'] as const)(
    'recovers an aged %s row when Bedrock accepted the last recovery attempt',
    async (status) => {
      const accepted = {
        ...document(103, status),
        ingestionRecoveryAttempts: 3,
      };
      ddb.on(QueryCommand).resolves({ Items: [accepted] });
      bedrock.on(GetKnowledgeBaseDocumentsCommand).resolves({
        documentDetails: [detail(accepted, 'IN_PROGRESS')],
      });

      await handleReconciler(schedule, context);

      expect(ddb.commandCalls(UpdateCommand)[0]?.args[0].input).toEqual(
        expect.objectContaining({
          ConditionExpression: '#status = :expected',
          ExpressionAttributeValues: expect.objectContaining({
            ':expected': status,
            ':status': 'INGESTING',
          }),
        }),
      );
      expect(s3.commandCalls(DeleteObjectCommand)).toHaveLength(0);
      expect(
        bedrock.commandCalls(IngestKnowledgeBaseDocumentsCommand),
      ).toHaveLength(0);
    },
  );

  it.each(['UPLOADING', 'PENDING'] as const)(
    'retries a completed %s upload when Bedrock did not accept ingestion',
    async (status) => {
      const uploaded = document(104, status);
      ddb.on(QueryCommand).resolves({ Items: [uploaded] });
      bedrock
        .on(GetKnowledgeBaseDocumentsCommand)
        .resolves({ documentDetails: [detail(uploaded, 'NOT_FOUND')] });
      s3.on(HeadObjectCommand).resolves({ ContentLength: 10 });

      await handleReconciler(schedule, context);

      expect(s3.commandCalls(DeleteObjectCommand)).toHaveLength(0);
      expect(
        bedrock.commandCalls(IngestKnowledgeBaseDocumentsCommand)[0]?.args[0]
          .input,
      ).toEqual(
        expect.objectContaining({
          clientToken: `ingest-${uploaded.documentId}`,
          documents: [
            expect.objectContaining({
              content: {
                dataSourceType: 'S3',
                s3: {
                  s3Location: { uri: `s3://test-documents/${uploaded.s3Key}` },
                },
              },
            }),
          ],
        }),
      );
      const writes = ddb.commandCalls(UpdateCommand);
      expect(
        writes.map(
          (call) => call.args[0].input.ExpressionAttributeValues?.[':status'],
        ),
      ).toEqual(['PENDING', 'INGESTING']);
      expect(
        writes[0]?.args[0].input.ExpressionAttributeValues?.[':attempt'],
      ).toBe(1);
    },
  );

  it('retains the file and leaves a bounded recovery retry pending after a transport failure', async () => {
    const uploaded = {
      ...document(105, 'PENDING'),
      ingestionRecoveryAttempts: 1,
    };
    ddb.on(QueryCommand).resolves({ Items: [uploaded] });
    bedrock
      .on(GetKnowledgeBaseDocumentsCommand)
      .resolves({ documentDetails: [detail(uploaded, 'NOT_FOUND')] });
    bedrock
      .on(IngestKnowledgeBaseDocumentsCommand)
      .rejects(new Error('temporary outage'));
    s3.on(HeadObjectCommand).resolves({ ContentLength: 10 });

    await expect(handleReconciler(schedule, context)).rejects.toThrow(
      '1 document(s) could not be reconciled',
    );

    expect(s3.commandCalls(DeleteObjectCommand)).toHaveLength(0);
    expect(ddb.commandCalls(UpdateCommand)).toHaveLength(1);
    expect(
      ddb.commandCalls(UpdateCommand)[0]?.args[0].input
        .ExpressionAttributeValues,
    ).toEqual(expect.objectContaining({ ':status': 'PENDING', ':attempt': 2 }));
  });

  it('retains a completed upload when recovery attempts are exhausted', async () => {
    const uploaded = {
      ...document(106, 'PENDING'),
      ingestionRecoveryAttempts: 3,
    };
    ddb.on(QueryCommand).resolves({ Items: [uploaded] });
    bedrock
      .on(GetKnowledgeBaseDocumentsCommand)
      .resolves({ documentDetails: [detail(uploaded, 'NOT_FOUND')] });
    s3.on(HeadObjectCommand).resolves({ ContentLength: 10 });

    await handleReconciler(schedule, context);

    expect(
      bedrock.commandCalls(IngestKnowledgeBaseDocumentsCommand),
    ).toHaveLength(0);
    expect(s3.commandCalls(DeleteObjectCommand)).toHaveLength(0);
    expect(
      ddb.commandCalls(UpdateCommand)[0]?.args[0].input
        .ExpressionAttributeValues,
    ).toEqual(
      expect.objectContaining({
        ':status': 'FAILED',
        ':reason': expect.stringContaining('retained'),
      }),
    );
  });

  it('does not ingest a completed upload above the actual byte limit', async () => {
    const uploaded = document(107, 'UPLOADING');
    ddb.on(QueryCommand).resolves({ Items: [uploaded] });
    bedrock
      .on(GetKnowledgeBaseDocumentsCommand)
      .resolves({ documentDetails: [detail(uploaded, 'NOT_FOUND')] });
    s3.on(HeadObjectCommand).resolves({ ContentLength: 26_214_401 });

    await handleReconciler(schedule, context);

    expect(
      bedrock.commandCalls(IngestKnowledgeBaseDocumentsCommand),
    ).toHaveLength(0);
    expect(
      s3.commandCalls(DeleteObjectCommand).map((call) => call.args[0].input.Key),
    ).toEqual([uploaded.s3Key, `${uploaded.s3Key}.metadata.json`]);
    expect(
      s3.commandCalls(DeleteObjectCommand)[1]?.calledBefore(
        ddb.commandCalls(UpdateCommand)[0]!,
      ),
    ).toBe(true);
    expect(
      ddb.commandCalls(UpdateCommand)[0]?.args[0].input
        .ExpressionAttributeValues?.[':status'],
    ).toBe('FAILED');
  });

  it.each([403, 503])(
    'does not delete or fail a file when S3 HEAD returns %s',
    async (httpStatusCode) => {
      const uploaded = document(108, 'UPLOADING');
      ddb.on(QueryCommand).resolves({ Items: [uploaded] });
      bedrock
        .on(GetKnowledgeBaseDocumentsCommand)
        .resolves({ documentDetails: [detail(uploaded, 'NOT_FOUND')] });
      s3.on(HeadObjectCommand).rejects({
        name: 'ServiceError',
        $metadata: { httpStatusCode },
      });

      await expect(handleReconciler(schedule, context)).rejects.toThrow(
        '1 document(s) could not be reconciled',
      );

      expect(s3.commandCalls(DeleteObjectCommand)).toHaveLength(0);
      expect(ddb.commandCalls(UpdateCommand)).toHaveLength(0);
    },
  );

  it('checks for an abandoned upload when Bedrock omits the document', async () => {
    const abandoned = document(109, 'UPLOADING');
    ddb.on(QueryCommand).resolves({ Items: [abandoned] });

    await handleReconciler(schedule, context);

    expect(s3.commandCalls(HeadObjectCommand)).toHaveLength(1);
    expect(
      ddb.commandCalls(UpdateCommand)[0]?.args[0].input.ExpressionAttributeValues,
    ).toEqual(expect.objectContaining({ ':status': 'FAILED' }));
    expect(s3.commandCalls(DeleteObjectCommand)).toHaveLength(2);
  });

  it('resubmits an omitted completed upload with the same idempotency token', async () => {
    const uploaded = document(112, 'UPLOADING');
    ddb.on(QueryCommand).resolves({ Items: [uploaded] });
    s3.on(HeadObjectCommand).resolves({ ContentLength: 10 });

    await handleReconciler(schedule, context);

    expect(
      bedrock.commandCalls(IngestKnowledgeBaseDocumentsCommand)[0]?.args[0].input
        .clientToken,
    ).toBe(`ingest-${uploaded.documentId}`);
    expect(s3.commandCalls(DeleteObjectCommand)).toHaveLength(0);
    expect(
      ddb.commandCalls(UpdateCommand).map(
        (call) => call.args[0].input.ExpressionAttributeValues?.[':status'],
      ),
    ).toEqual(['PENDING', 'INGESTING']);
  });

  it('does not declare exhausted recovery failed without a definite missing Bedrock status', async () => {
    const uploaded = {
      ...document(113, 'UPLOADING'),
      ingestionRecoveryAttempts: 3,
    };
    ddb.on(QueryCommand).resolves({ Items: [uploaded] });
    s3.on(HeadObjectCommand).resolves({ ContentLength: 10 });

    await handleReconciler(schedule, context);

    expect(ddb.commandCalls(UpdateCommand)).toHaveLength(0);
    expect(s3.commandCalls(DeleteObjectCommand)).toHaveLength(0);
    expect(
      bedrock.commandCalls(IngestKnowledgeBaseDocumentsCommand),
    ).toHaveLength(0);
  });

  it.each(['PENDING', 'INGESTING'] as const)(
    'leaves an omitted %s document untouched',
    async (status) => {
      ddb.on(QueryCommand).resolves({ Items: [document(114, status)] });

      await handleReconciler(schedule, context);

      expect(s3.calls()).toHaveLength(0);
      expect(ddb.commandCalls(UpdateCommand)).toHaveLength(0);
    },
  );

  it('continues within and beyond a batch after one upload HEAD is denied, then reports failure', async () => {
    const blocked = document(115, 'UPLOADING');
    const indexed = Array.from({ length: 11 }, (_, index) =>
      document(116 + index, 'INGESTING'),
    );
    const items = [blocked, ...indexed];
    ddb.on(QueryCommand).resolves({ Items: items });
    bedrock.on(GetKnowledgeBaseDocumentsCommand)
      .resolvesOnce({
        documentDetails: [
          detail(blocked, 'NOT_FOUND'),
          ...indexed.slice(0, 9).map((item) => detail(item, 'INDEXED')),
        ],
      })
      .resolvesOnce({
        documentDetails: indexed.slice(9).map((item) => detail(item, 'INDEXED')),
      });
    s3.on(HeadObjectCommand).rejects({
      name: 'AccessDenied',
      $metadata: { httpStatusCode: 403 },
    });
    const logs = vi.spyOn(console, 'log');

    const failure: unknown = await handleReconciler(schedule, context).catch(
      (error: unknown) => error,
    );

    expect(ddb.commandCalls(UpdateCommand)).toHaveLength(11);
    expect(bedrock.commandCalls(GetKnowledgeBaseDocumentsCommand)).toHaveLength(2);
    expect(s3.commandCalls(DeleteObjectCommand)).toHaveLength(0);
    expect(failure).toEqual(
      expect.objectContaining({ message: '1 document(s) could not be reconciled' }),
    );
    expect(
      logs.mock.calls.map(([entry]) => JSON.parse(String(entry)) as unknown),
    ).toContainEqual(
      expect.objectContaining({ msg: 'reconciler summary', errored: 1, ready: 11 }),
    );
  });

  it('continues after a document state write fails', async () => {
    const blocked = document(127, 'INGESTING');
    const indexed = document(128, 'INGESTING');
    ddb.on(QueryCommand).resolves({ Items: [blocked, indexed] });
    bedrock.on(GetKnowledgeBaseDocumentsCommand).resolves({
      documentDetails: [detail(blocked, 'INDEXED'), detail(indexed, 'INDEXED')],
    });
    ddb.on(UpdateCommand, { Key: { pk: blocked.pk, sk: 'META' } })
      .rejects(new Error('temporary DynamoDB failure'));

    const failure: unknown = await handleReconciler(schedule, context).catch(
      (error: unknown) => error,
    );

    expect(
      ddb.commandCalls(UpdateCommand, { Key: { pk: indexed.pk, sk: 'META' } }),
    ).toHaveLength(1);
    expect(failure).toEqual(
      expect.objectContaining({ message: '1 document(s) could not be reconciled' }),
    );
  });

  it('continues polling later batches after a Bedrock batch fails', async () => {
    const items = Array.from({ length: 11 }, (_, index) =>
      document(129 + index, 'INGESTING'),
    );
    ddb.on(QueryCommand).resolves({ Items: items });
    bedrock.on(GetKnowledgeBaseDocumentsCommand)
      .rejectsOnce(new Error('temporary Bedrock failure'))
      .resolvesOnce({ documentDetails: [detail(items[10]!, 'INDEXED')] });
    const logs = vi.spyOn(console, 'log');

    const failure: unknown = await handleReconciler(schedule, context).catch(
      (error: unknown) => error,
    );

    expect(
      ddb.commandCalls(UpdateCommand, { Key: { pk: items[10]!.pk, sk: 'META' } }),
    ).toHaveLength(1);
    expect(failure).toEqual(
      expect.objectContaining({ message: '10 document(s) could not be reconciled' }),
    );
    expect(
      logs.mock.calls.map(([entry]) => JSON.parse(String(entry)) as unknown),
    ).toContainEqual(
      expect.objectContaining({ msg: 'reconciler summary', errored: 10, ready: 1 }),
    );
  });

  it.each(['object', 'sidecar'])(
    'leaves oversized cleanup retryable when deleting the %s fails',
    async (which) => {
      const uploaded = document(140, 'UPLOADING');
      ddb.on(QueryCommand).resolves({ Items: [uploaded] });
      bedrock.on(GetKnowledgeBaseDocumentsCommand)
        .resolves({ documentDetails: [detail(uploaded, 'NOT_FOUND')] });
      s3.on(HeadObjectCommand).resolves({ ContentLength: 26_214_401 });
      s3.on(DeleteObjectCommand, {
        Key: which === 'object' ? uploaded.s3Key : `${uploaded.s3Key}.metadata.json`,
      }).rejects(new Error('temporary S3 failure'));

      await expect(handleReconciler(schedule, context)).rejects.toThrow(
        '1 document(s) could not be reconciled',
      );

      expect(s3.commandCalls(DeleteObjectCommand)).toHaveLength(2);
      expect(ddb.commandCalls(UpdateCommand)).toHaveLength(0);
      expect(
        bedrock.commandCalls(IngestKnowledgeBaseDocumentsCommand),
      ).toHaveLength(0);
    },
  );

  it('does not retry ingestion after losing a conditional recovery claim', async () => {
    const uploaded = document(110, 'UPLOADING');
    ddb.on(QueryCommand).resolves({ Items: [uploaded] });
    bedrock
      .on(GetKnowledgeBaseDocumentsCommand)
      .resolves({ documentDetails: [detail(uploaded, 'NOT_FOUND')] });
    s3.on(HeadObjectCommand).resolves({ ContentLength: 10 });
    ddb
      .on(UpdateCommand)
      .rejects(
        new ConditionalCheckFailedException({
          message: 'changed',
          $metadata: {},
        }),
      );

    await handleReconciler(schedule, context);

    expect(
      bedrock.commandCalls(IngestKnowledgeBaseDocumentsCommand),
    ).toHaveLength(0);
    expect(s3.commandCalls(DeleteObjectCommand)).toHaveLength(0);
  });

  it('surfaces a failed recovery state write instead of treating it as a concurrent update', async () => {
    const uploaded = document(111, 'UPLOADING');
    ddb.on(QueryCommand).resolves({ Items: [uploaded] });
    bedrock
      .on(GetKnowledgeBaseDocumentsCommand)
      .resolves({ documentDetails: [detail(uploaded, 'NOT_FOUND')] });
    s3.on(HeadObjectCommand).resolves({ ContentLength: 10 });
    ddb.on(UpdateCommand).rejects(new Error('temporary DynamoDB failure'));

    await expect(handleReconciler(schedule, context)).rejects.toThrow(
      '1 document(s) could not be reconciled',
    );

    expect(
      bedrock.commandCalls(IngestKnowledgeBaseDocumentsCommand),
    ).toHaveLength(0);
    expect(s3.commandCalls(DeleteObjectCommand)).toHaveLength(0);
  });
});
