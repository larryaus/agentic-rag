import { GetKnowledgeBaseDocumentsCommand } from '@aws-sdk/client-bedrock-agent';
import { DeleteObjectCommand } from '@aws-sdk/client-s3';
import { QueryCommand, UpdateCommand } from '@aws-sdk/lib-dynamodb';
import type { Context, ScheduledEvent } from 'aws-lambda';

import { bedrockAgentClient, dynamoClient, s3Client } from '../lib/clients';
import { loadReconcilerConfig } from '../lib/config';
import { documentPk, type DocumentItem } from '../lib/ddb';
import { errorMessage } from '../lib/errors';
import { log, withLogContext } from '../lib/logger';

const cfg = loadReconcilerConfig();
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
}): Promise<boolean> {
  try {
    await dynamoClient.send(
      new UpdateCommand({
        TableName: cfg.tableName,
        Key: { pk: documentPk(opts.item.documentId), sk: 'META' },
        UpdateExpression:
          opts.reason === undefined
            ? 'SET #status = :status, updatedAt = :updatedAt REMOVE errorMessage'
            : 'SET #status = :status, updatedAt = :updatedAt, errorMessage = :reason',
        ConditionExpression: '#status = :expected',
        ExpressionAttributeNames: { '#status': 'status' },
        ExpressionAttributeValues: {
          ':status': opts.status,
          ':expected': opts.expected,
          ':updatedAt': new Date().toISOString(),
          ...(opts.reason === undefined
            ? {}
            : { ':reason': opts.reason.slice(0, 1000) }),
        },
      }),
    );
    return true;
  } catch {
    return false;
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
    };
    log('info', 'reconciler run started');
    try {
      const documents = await listDocuments();
      counts.examined = documents.length;
      const cutoff =
        Date.now() - cfg.abandonedUploadMinutes * 60 * 1000;
      const recoveredUploads = new Set<string>();
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
            if (item.status === 'UPLOADING') {
              recoveredUploads.add(item.documentId);
            }
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
            TERMINAL_FAILURES.has(status) ||
            status.includes('PARTIAL')
          ) {
            if (item.status === 'UPLOADING') {
              // NOT_FOUND is the expected result for an abandoned presign.
              // Leave it selectable until S3 cleanup succeeds below.
              continue;
            }
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
            item.status === 'UPLOADING' &&
            ACTIVE_INGESTION_STATUSES.has(status)
          ) {
            recoveredUploads.add(item.documentId);
            if (
              await transition({
                item,
                expected: 'UPLOADING',
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

      const abandoned = documents.filter(
        (document) =>
          document.status === 'UPLOADING' &&
          !recoveredUploads.has(document.documentId) &&
          Date.parse(document.uploadedAt) < cutoff,
      );
      for (const item of abandoned) {
        const changed = await transition({
          item,
          expected: 'UPLOADING',
          status: 'FAILED',
          reason: 'Upload was not completed before the presigned URL expired',
        });
        if (!changed) {
          counts.concurrent += 1;
          log('info', 'abandoned upload advanced before cleanup', {
            documentId: item.documentId,
          });
          continue;
        }
        counts.abandoned += 1;
        counts.failed += 1;
        await deleteUploadObjects(item);
      }
      log('info', 'reconciler summary', counts);
    } catch (error) {
      log('error', 'reconciler run failed', { error: errorMessage(error) });
    } finally {
      log('info', 'reconciler run completed', {
        durationMs: Date.now() - started,
      });
    }
  });
}

export const handler = handleReconciler;
