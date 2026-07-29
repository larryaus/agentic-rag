import { IngestKnowledgeBaseDocumentsCommand } from '@aws-sdk/client-bedrock-agent';
import { DeleteObjectCommand } from '@aws-sdk/client-s3';
import { UpdateCommand } from '@aws-sdk/lib-dynamodb';
import type { Context, EventBridgeEvent } from 'aws-lambda';

import { bedrockAgentClient, dynamoClient, s3Client } from '../lib/clients';
import { loadIngestConfig } from '../lib/config';
import {
  documentPk,
  getDocument,
  type DocumentItem,
} from '../lib/ddb';
import { errorMessage } from '../lib/errors';
import { log, withLogContext } from '../lib/logger';

type ObjectCreatedDetail = {
  bucket: { name: string };
  object: { key: string; size: number };
};

const cfg = loadIngestConfig();
const DOCUMENT_KEY =
  /^uploads\/([^/]+)\/([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\/(.+)$/i;

function decodeObjectKey(raw: string): string | undefined {
  try {
    return decodeURIComponent(raw.replace(/\+/g, ' '));
  } catch {
    return undefined;
  }
}

async function updateStatus(opts: {
  documentId: string;
  expected: readonly DocumentItem['status'][];
  status: DocumentItem['status'];
  error?: string;
}): Promise<void> {
  const expectedTokens = opts.expected.map(
    (_status, index) => `:expected${index}`,
  );
  await dynamoClient.send(
    new UpdateCommand({
      TableName: cfg.tableName,
      Key: { pk: documentPk(opts.documentId), sk: 'META' },
      UpdateExpression:
        opts.error === undefined
          ? 'SET #status = :status, updatedAt = :updatedAt REMOVE errorMessage'
          : 'SET #status = :status, updatedAt = :updatedAt, errorMessage = :error',
      ConditionExpression:
        expectedTokens.length === 1
          ? `#status = ${expectedTokens[0]}`
          : `#status IN (${expectedTokens.join(', ')})`,
      ExpressionAttributeNames: { '#status': 'status' },
      ExpressionAttributeValues: {
        ':status': opts.status,
        ':updatedAt': new Date().toISOString(),
        ...Object.fromEntries(
          opts.expected.map((status, index) => [
            `:expected${index}`,
            status,
          ]),
        ),
        ...(opts.error === undefined ? {} : { ':error': opts.error.slice(0, 1000) }),
      },
    }),
  );
}

async function deleteUploadObjects(key: string): Promise<boolean> {
  const keys = [key, `${key}.metadata.json`];
  const results = await Promise.allSettled(
    keys.map((objectKey) =>
      s3Client.send(
        new DeleteObjectCommand({
          Bucket: cfg.docsBucket,
          Key: objectKey,
        }),
      ),
    ),
  );
  let deleted = true;
  results.forEach((result, index) => {
    if (result.status === 'rejected') {
      deleted = false;
      log('warn', 'failed to delete rejected upload object', {
        key: keys[index],
        error: errorMessage(result.reason),
      });
    }
  });
  return deleted;
}

export async function handleIngest(
  event: EventBridgeEvent<'Object Created', ObjectCreatedDetail>,
  context: Context,
): Promise<void> {
  await withLogContext({ requestId: context.awsRequestId }, async () => {
    const started = Date.now();
    log('info', 'ingest event started');
    try {
      const key = decodeObjectKey(event.detail.object.key);
      if (key === undefined) {
        log('warn', 'ignoring undecodable object key');
        return;
      }
      if (key.endsWith('.metadata.json')) {
        log('info', 'ignoring metadata sidecar');
        return;
      }
      const match = DOCUMENT_KEY.exec(key);
      if (match === null) {
        log('warn', 'ignoring object outside upload key layout', { key });
        return;
      }
      const documentId = match[2];
      if (documentId === undefined) {
        return;
      }
      const document = await getDocument({
        tableName: cfg.tableName,
        documentId,
      });
      if (document === undefined) {
        log('warn', 'ignoring upload event without a document record', {
          documentId,
          key,
        });
        return;
      }
      if (document.status !== 'UPLOADING') {
        log('info', 'ignoring upload event for document already processed', {
          documentId,
          key,
          status: document.status,
        });
        return;
      }
      if (event.detail.object.size > cfg.maxUploadBytes) {
        const deleted = await deleteUploadObjects(key);
        if (!deleted) {
          // Keep the row selectable so the reconciler retries both deletions.
          return;
        }
        await updateStatus({
          documentId,
          expected: ['UPLOADING'],
          status: 'FAILED',
          error: `Object exceeds the ${cfg.maxUploadBytes} byte upload limit`,
        });
        return;
      }

      try {
        await bedrockAgentClient.send(
          new IngestKnowledgeBaseDocumentsCommand({
            knowledgeBaseId: cfg.knowledgeBaseId,
            dataSourceId: cfg.dataSourceId,
            clientToken: `ingest-${documentId}`,
            documents: [
              {
                content: {
                  dataSourceType: 'S3',
                  s3: { s3Location: { uri: `s3://${cfg.docsBucket}/${key}` } },
                },
                metadata: {
                  type: 'S3_LOCATION',
                  s3Location: {
                    uri: `s3://${cfg.docsBucket}/${key}.metadata.json`,
                  },
                },
              },
            ],
          }),
        );
      } catch (error) {
        // A transport failure can arrive after Bedrock accepted the request.
        // Keep the row pollable; retries reuse the same idempotency token.
        log('warn', 'ingestion request failed; leaving status for reconciliation', {
          documentId,
          error: errorMessage(error),
        });
        return;
      }

      try {
        await updateStatus({
          documentId,
          expected: ['UPLOADING', 'PENDING'],
          status: 'INGESTING',
        });
      } catch (error) {
        log('warn', 'Bedrock accepted ingestion but status update failed', {
          documentId,
          error: errorMessage(error),
        });
        try {
          // PENDING is a recovery marker: aged rows are polled by the
          // reconciler, which can distinguish accepted ingestion from NOT_FOUND.
          await updateStatus({
            documentId,
            expected: ['UPLOADING', 'PENDING'],
            status: 'PENDING',
          });
        } catch (recoveryError) {
          log('warn', 'failed to leave ingestion pending for reconciliation', {
            documentId,
            error: errorMessage(recoveryError),
          });
        }
      }
    } catch (error) {
      log('error', 'ingest event failed without retry', {
        error: errorMessage(error),
      });
    } finally {
      log('info', 'ingest event completed', {
        durationMs: Date.now() - started,
      });
    }
  });
}

export const handler = handleIngest;
