import { ConditionalCheckFailedException } from '@aws-sdk/client-dynamodb';
import { DeleteObjectCommand } from '@aws-sdk/client-s3';
import { DeleteCommand, QueryCommand } from '@aws-sdk/lib-dynamodb';
import type {
  APIGatewayProxyEventV2WithJWTAuthorizer,
  APIGatewayProxyStructuredResultV2,
  Context,
} from 'aws-lambda';
import type { DocumentSummary, Page } from '@kb/shared';

import { dynamoClient, s3Client } from '../lib/clients';
import { loadDocumentsConfig } from '../lib/config';
import {
  decodePageToken,
  documentPk,
  encodePageToken,
  getDocument,
  type DocumentItem,
} from '../lib/ddb';
import {
  ConflictError,
  errorMessage,
  NotFoundError,
  ValidationError,
} from '../lib/errors';
import {
  authFromHttpApi,
  errorResponse,
  jsonResponse,
} from '../lib/http';
import { log, setLogUser, withLogContext } from '../lib/logger';
import { presignGet } from '../lib/presigner';

const cfg = loadDocumentsConfig();
const LIST_CURSOR_KEYS = ['pk', 'sk', 'gsi1pk', 'gsi1sk'] as const;

function parseLimit(raw: string | undefined, fallback: number): number {
  if (raw === undefined) {
    return fallback;
  }
  const value = Number(raw);
  if (!Number.isInteger(value) || value <= 0 || value > 100) {
    throw new ValidationError('limit must be between 1 and 100');
  }
  return value;
}

function summary(item: DocumentItem): DocumentSummary {
  return {
    documentId: item.documentId,
    title: item.title,
    contentType: item.contentType,
    sizeBytes: item.sizeBytes,
    status: item.status,
    uploadedAt: item.uploadedAt,
  };
}

async function listDocuments(
  event: APIGatewayProxyEventV2WithJWTAuthorizer,
): Promise<APIGatewayProxyStructuredResultV2> {
  const limit = parseLimit(event.queryStringParameters?.limit, 25);
  const token = event.queryStringParameters?.nextToken;
  const exclusiveStartKey =
    token === undefined
      ? undefined
      : decodePageToken(token, {
          expectedKeys: LIST_CURSOR_KEYS,
          partition: { key: 'gsi1pk', value: 'ORG#DOCUMENT' },
        });
  const output = await dynamoClient.send(
    new QueryCommand({
      TableName: cfg.tableName,
      IndexName: 'gsi1',
      KeyConditionExpression: 'gsi1pk = :partition',
      ExpressionAttributeValues: { ':partition': 'ORG#DOCUMENT' },
      ScanIndexForward: false,
      Limit: limit,
      ...(exclusiveStartKey === undefined
        ? {}
        : { ExclusiveStartKey: exclusiveStartKey }),
    }),
  );
  const response: Page<DocumentSummary> = {
    items: ((output.Items ?? []) as DocumentItem[]).map(summary),
    ...(output.LastEvaluatedKey === undefined
      ? {}
      : {
          nextToken: encodePageToken(
            output.LastEvaluatedKey as Record<string, string>,
          ),
        }),
  };
  return jsonResponse(200, response);
}

async function downloadDocument(
  documentId: string | undefined,
): Promise<APIGatewayProxyStructuredResultV2> {
  if (documentId === undefined || documentId === '') {
    throw new ValidationError('documentId is required');
  }
  const item = await getDocument({ tableName: cfg.tableName, documentId });
  if (item === undefined) {
    throw new NotFoundError('Document not found');
  }
  if (item.status !== 'READY') {
    throw new ConflictError('Document is not ready');
  }
  // Documents are org-shared by design, so any authenticated user may download a ready source.
  const url = await presignGet({
    bucket: cfg.docsBucket,
    key: item.s3Key,
    expiresIn: 300,
  });
  return jsonResponse(200, { url });
}

async function removeFailedDocument(
  documentId: string | undefined,
): Promise<APIGatewayProxyStructuredResultV2> {
  if (documentId === undefined || documentId === '') {
    throw new ValidationError('documentId is required');
  }
  const item = await getDocument({ tableName: cfg.tableName, documentId });
  if (item === undefined) {
    throw new NotFoundError('Document not found');
  }
  if (item.status !== 'FAILED') {
    throw new ConflictError('Only failed documents can be removed');
  }
  // Failed documents were never indexed, so there is nothing to retract from the
  // knowledge base and, like the rest of the shared library, any user may clear them.
  try {
    await dynamoClient.send(
      new DeleteCommand({
        TableName: cfg.tableName,
        Key: { pk: documentPk(documentId), sk: 'META' },
        ConditionExpression: '#status = :failed',
        ExpressionAttributeNames: { '#status': 'status' },
        ExpressionAttributeValues: { ':failed': 'FAILED' },
      }),
    );
  } catch (error) {
    if (error instanceof ConditionalCheckFailedException) {
      throw new ConflictError('Only failed documents can be removed');
    }
    throw error;
  }
  const keys = [item.s3Key, `${item.s3Key}.metadata.json`];
  const results = await Promise.allSettled(
    keys.map((key) =>
      s3Client.send(
        new DeleteObjectCommand({ Bucket: cfg.docsBucket, Key: key }),
      ),
    ),
  );
  results.forEach((result, index) => {
    if (result.status === 'rejected') {
      log('warn', 'failed to delete removed document object', {
        key: keys[index],
        error: errorMessage(result.reason),
      });
    }
  });
  return { statusCode: 204 };
}

export async function handleDocuments(
  event: APIGatewayProxyEventV2WithJWTAuthorizer,
  context: Context,
): Promise<APIGatewayProxyStructuredResultV2> {
  return withLogContext({ requestId: context.awsRequestId }, async () => {
    const started = Date.now();
    log('info', 'documents request started');
    try {
      const auth = authFromHttpApi(event);
      setLogUser(auth.sub);
      if (event.routeKey === 'GET /v1/documents') {
        return await listDocuments(event);
      }
      if (
        event.routeKey ===
        'GET /v1/documents/{documentId}/download'
      ) {
        return await downloadDocument(event.pathParameters?.documentId);
      }
      if (event.routeKey === 'DELETE /v1/documents/{documentId}') {
        return await removeFailedDocument(event.pathParameters?.documentId);
      }
      throw new NotFoundError();
    } catch (error) {
      return errorResponse(error);
    } finally {
      log('info', 'documents request completed', {
        durationMs: Date.now() - started,
      });
    }
  });
}

export const handler = handleDocuments;
