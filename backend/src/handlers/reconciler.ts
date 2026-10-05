import { GetKnowledgeBaseDocumentsCommand } from '@aws-sdk/client-bedrock-agent';
import { ConditionalCheckFailedException } from '@aws-sdk/client-dynamodb';
import { DeleteObjectCommand, HeadObjectCommand } from '@aws-sdk/client-s3';
import { QueryCommand, UpdateCommand } from '@aws-sdk/lib-dynamodb';
import type { Context, ScheduledEvent } from 'aws-lambda';

import { bedrockAgentClient, dynamoClient, s3Client } from '../lib/clients';
import { loadReconcilerConfig } from '../lib/config';
import { documentPk, type DocumentItem } from '../lib/ddb';
import { errorMessage } from '../lib/errors';
import { ingestDocument } from '../lib/ingestion';
import { log, withLogContext } from '../lib/logger';

const cfg = loadReconcilerConfig();
const MAX_RECOVERY_ATTEMPTS = 3;
const TERMINAL_FAILURES = new Set([
  'FAILED',
  'NOT_FOUND',
  'IGNORED',
  'METADATA_UPDATE_FAILED',
]);
const ACTIVE_INGESTION_STATUSES = new Set([
  'IN_PROGRESS',
  'STARTING',
  'PENDING',
]);

type Counts = {
  examined: number;
  ready: number;
  failed: number;
  abandoned: number;
  unchanged: number;
  concurrent: number;
  retried: number;
};

async function listDocuments(): Promise<DocumentItem[]> {
  const documents: DocumentItem[] = [];
  let cursor: Record<string, unknown> | undefined;
  do {
    const output = await dynamoClient.send(
      new QueryCommand({
        TableName: cfg.tableName,
        IndexName: 'gsi1',
        KeyConditionExpression: 'gsi1pk = :partition',
        ExpressionAttributeValues: { ':partition': 'ORG#DOCUMENT' },
        ...(cursor === undefined ? {} : { ExclusiveStartKey: cursor }),
      }),
    );
    documents.push(...((output.Items ?? []) as DocumentItem[]));
    cursor = output.LastEvaluatedKey;
  } while (cursor !== undefined);
  return documents;
}

async function transition(opts: {
  item: DocumentItem;
  expected: string;
  status: string;
  reason?: string;
  recoveryAttempt?: number;
}): Promise<boolean> {
  const assignments = ['#status = :status', 'updatedAt = :updatedAt'];
  if (opts.reason !== undefined) assignments.push('errorMessage = :reason');
  if (opts.recoveryAttempt !== undefined) {
    assignments.push('ingestionRecoveryAttempts = :attempt');
  }
  try {
    await dynamoClient.send(
      new UpdateCommand({
        TableName: cfg.tableName,
        Key: { pk: documentPk(opts.item.documentId), sk: 'META' },
        UpdateExpression: `SET ${assignments.join(', ')}${opts.reason === undefined ? ' REMOVE errorMessage' : ''}`,
        ConditionExpression: '#status = :expected',
        ExpressionAttributeNames: { '#status': 'status' },
        ExpressionAttributeValues: {
          ':status': opts.status,
          ':expected': opts.expected,
          ':updatedAt': new Date().toISOString(),
          ...(opts.reason === undefined
            ? {}
            : { ':reason': opts.reason.slice(0, 1000) }),
          ...(opts.recoveryAttempt === undefined
            ? {}
            : { ':attempt': opts.recoveryAttempt }),
        },
      }),
    );
    return true;
  } catch (error) {
    if (error instanceof ConditionalCheckFailedException) return false;
    throw error;
  }
}

function groupsOfTen<T>(items: T[]): T[][] {
  const groups: T[][] = [];
  for (let index = 0; index < items.length; index += 10) {
    groups.push(items.slice(index, index + 10));
  }
  return groups;
}

async function deleteUploadObjects(item: DocumentItem): Promise<boolean> {
  const keys = [item.s3Key, `${item.s3Key}.metadata.json`];
  const results = await Promise.allSettled(
    keys.map((key) =>
      s3Client.send(
        new DeleteObjectCommand({
          Bucket: cfg.docsBucket,
          Key: key,
        }),
      ),
    ),
  );
  let deleted = true;
  results.forEach((result, index) => {
    if (result.status === 'rejected') {
      deleted = false;
      log('warn', 'failed to clean up abandoned upload object', {
        documentId: item.documentId,
        key: keys[index],
        error: errorMessage(result.reason),
      });
    }
  });
  return deleted;
}

async function recoverMissingIngestion(
  item: DocumentItem,
  counts: Counts,
): Promise<void> {
  let size: number | undefined;
  try {
    const object = await s3Client.send(
      new HeadObjectCommand({ Bucket: cfg.docsBucket, Key: item.s3Key }),
    );
    size = object.ContentLength;
    if (size === undefined) {
      throw new Error('S3 omitted the uploaded object size');
    }
  } catch (error) {
    // Only a definite missing object establishes an abandoned upload. A denied
    // or unavailable HEAD must never authorize deleting an uploaded document.
    if (
      typeof error !== 'object' ||
      error === null ||
      !('$metadata' in error) ||
      (error.$metadata as { httpStatusCode?: number } | undefined)
        ?.httpStatusCode !== 404
    ) {
      throw error;
    }
    if (
      await transition({
        item,
        expected: item.status,
        status: 'FAILED',
        reason: 'Upload was not completed before the presigned URL expired',
      })
    ) {
      counts.abandoned += 1;
      counts.failed += 1;
      await deleteUploadObjects(item);
    } else {
      counts.concurrent += 1;
    }
    return;
  }

  const attempts = item.ingestionRecoveryAttempts ?? 0;
  if (size > cfg.maxUploadBytes || attempts >= MAX_RECOVERY_ATTEMPTS) {
    const reason =
      size > cfg.maxUploadBytes
        ? `Object exceeds the ${cfg.maxUploadBytes} byte upload limit`
        : 'Ingestion did not start after three recovery attempts; the uploaded file has been retained';
    if (
      await transition({
        item,
        expected: item.status,
        status: 'FAILED',
        reason,
      })
    ) {
      counts.failed += 1;
    } else {
      counts.concurrent += 1;
    }
    return;
  }

  // Claim the row before submission so a delayed S3 event cannot race recovery.
  if (
    !(await transition({
      item,
      expected: item.status,
      status: 'PENDING',
      recoveryAttempt: attempts + 1,
    }))
  ) {
    counts.concurrent += 1;
    return;
  }
  try {
    await ingestDocument({
      ...cfg,
      documentId: item.documentId,
      key: item.s3Key,
    });
  } catch (error) {
    // The request may have been accepted. Leave PENDING for status polling,
    // then retry only if Bedrock reports NOT_FOUND after the recovery interval.
    log('warn', 'ingestion recovery request failed; retaining uploaded file', {
      documentId: item.documentId,
      error: errorMessage(error),
    });
    return;
  }
  counts.retried += 1;
  if (!(await transition({ item, expected: 'PENDING', status: 'INGESTING' }))) {
    counts.concurrent += 1;
  }
}

export async function handleReconciler(
  _event: ScheduledEvent,
  context: Context,
): Promise<void> {
  await withLogContext({ requestId: context.awsRequestId }, async () => {
    const started = Date.now();
    const counts: Counts = {
      examined: 0,
      ready: 0,
      failed: 0,
      abandoned: 0,
      unchanged: 0,
      concurrent: 0,
      retried: 0,
    };
    log('info', 'reconciler run started');
    try {
      const documents = await listDocuments();
      counts.examined = documents.length;
      const cutoff = Date.now() - cfg.abandonedUploadMinutes * 60 * 1000;
      const pollable = documents.filter(
        (document) =>
          document.status === 'INGESTING' ||
          (['PENDING', 'UPLOADING'].includes(document.status) &&
            Date.parse(document.updatedAt) < cutoff),
      );
      for (const batch of groupsOfTen(pollable)) {
        const response = await bedrockAgentClient.send(
          new GetKnowledgeBaseDocumentsCommand({
            knowledgeBaseId: cfg.knowledgeBaseId,
            dataSourceId: cfg.dataSourceId,
            documentIdentifiers: batch.map((document) => ({
              dataSourceType: 'S3',
              s3: { uri: `s3://${cfg.docsBucket}/${document.s3Key}` },
            })),
          }),
        );
        const byUri = new Map(
          batch.map((document) => [
            `s3://${cfg.docsBucket}/${document.s3Key}`,
            document,
          ]),
        );
        for (const detail of response.documentDetails ?? []) {
          const uri = detail.identifier?.s3?.uri;
          const item = uri === undefined ? undefined : byUri.get(uri);
          if (item === undefined) {
            continue;
          }
          const status = detail.status ?? '';
          if (status === 'INDEXED') {
            if (
              await transition({
                item,
                expected: item.status,
                status: 'READY',
              })
            ) {
              counts.ready += 1;
            } else {
              counts.concurrent += 1;
            }
          } else if (
            status === 'NOT_FOUND' &&
            ['UPLOADING', 'PENDING'].includes(item.status)
          ) {
            await recoverMissingIngestion(item, counts);
          } else if (
            TERMINAL_FAILURES.has(status) ||
            status.includes('PARTIAL')
          ) {
            const reason =
              detail.statusReason ??
              `Bedrock document entered terminal status ${status}`;
            if (
              await transition({
                item,
                expected: item.status,
                status: 'FAILED',
                reason,
              })
            ) {
              counts.failed += 1;
            } else {
              counts.concurrent += 1;
            }
          } else if (
            ['UPLOADING', 'PENDING'].includes(item.status) &&
            ACTIVE_INGESTION_STATUSES.has(status)
          ) {
            if (
              await transition({
                item,
                expected: item.status,
                status: 'INGESTING',
              })
            ) {
              counts.unchanged += 1;
            } else {
              counts.concurrent += 1;
            }
          } else {
            counts.unchanged += 1;
          }
        }
      }

      log('info', 'reconciler summary', counts);
    } catch (error) {
      log('error', 'reconciler run failed', { error: errorMessage(error) });
      throw error;
    } finally {
      log('info', 'reconciler run completed', {
        durationMs: Date.now() - started,
      });
    }
  });
}

export const handler = handleReconciler;
