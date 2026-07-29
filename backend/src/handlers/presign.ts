import { PutObjectCommand } from '@aws-sdk/client-s3';
import { PutCommand } from '@aws-sdk/lib-dynamodb';
import type {
  APIGatewayProxyEventV2WithJWTAuthorizer,
  APIGatewayProxyStructuredResultV2,
  Context,
} from 'aws-lambda';
import type { UploadRequest, UploadResponse } from '@kb/shared';
import { randomUUID } from 'node:crypto';

import { dynamoClient, s3Client } from '../lib/clients';
import { loadPresignConfig } from '../lib/config';
import { documentPk } from '../lib/ddb';
import { ValidationError } from '../lib/errors';
import {
  authFromHttpApi,
  errorResponse,
  jsonResponse,
  parseJsonBody,
} from '../lib/http';
import { log, setLogUser, withLogContext } from '../lib/logger';
import { presignPut } from '../lib/presigner';

const cfg = loadPresignConfig();
const EXPIRY_SECONDS = 300;
const CONTENT_TYPES: Readonly<Record<string, readonly string[]>> = {
  'application/pdf': ['.pdf'],
  'text/plain': ['.txt'],
  'text/markdown': ['.md'],
  'text/html': ['.html', '.htm'],
};

export function sanitizeFilename(filename: string): string {
  const basename = filename.split(/[\\/]/).at(-1) ?? '';
  return basename
    .replace(/[^A-Za-z0-9._-]+/g, '')
    .replace(/([._-])\1+/g, '$1')
    .slice(0, 128);
}

function validateUpload(value: unknown): UploadRequest & { safeName: string } {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new ValidationError();
  }
  const body = value as Record<string, unknown>;
  if (
    typeof body.filename !== 'string' ||
    typeof body.contentType !== 'string' ||
    typeof body.sizeBytes !== 'number'
  ) {
    throw new ValidationError('filename, contentType, and sizeBytes are required');
  }
  if (
    !Number.isInteger(body.sizeBytes) ||
    body.sizeBytes <= 0 ||
    body.sizeBytes > cfg.maxUploadBytes
  ) {
    throw new ValidationError('Invalid file size');
  }
  const safeName = sanitizeFilename(body.filename);
  if (
    safeName === '' ||
    safeName === '.' ||
    safeName === '..' ||
    safeName.endsWith('.metadata.json')
  ) {
    throw new ValidationError('Invalid filename');
  }
  const allowedExtensions = CONTENT_TYPES[body.contentType];
  const lowerName = safeName.toLowerCase();
  if (
    allowedExtensions === undefined ||
    !allowedExtensions.some((extension) => lowerName.endsWith(extension))
  ) {
    throw new ValidationError('File extension does not match content type');
  }
  return {
    filename: body.filename,
    contentType: body.contentType,
    sizeBytes: body.sizeBytes,
    safeName,
  };
}

export async function handlePresign(
  event: APIGatewayProxyEventV2WithJWTAuthorizer,
  context: Context,
): Promise<APIGatewayProxyStructuredResultV2> {
  return withLogContext({ requestId: context.awsRequestId }, async () => {
    const started = Date.now();
    log('info', 'presign request started');
    try {
      const auth = authFromHttpApi(event);
      setLogUser(auth.sub);
      const upload = validateUpload(parseJsonBody(event));
      const documentId = randomUUID();
      const key = `uploads/${auth.sub}/${documentId}/${upload.safeName}`;
      const now = new Date();
      const uploadedAt = now.toISOString();
      const sidecar = {
        metadataAttributes: {
          documentId: {
            value: { type: 'STRING', stringValue: documentId },
            includeForEmbedding: false,
          },
          uploaderSub: {
            value: { type: 'STRING', stringValue: auth.sub },
            includeForEmbedding: false,
          },
          // Metadata must exist at ingestion time. Stage 2 uses this numeric value for range filters.
          uploadedAt: {
            value: {
              type: 'NUMBER',
              numberValue: Math.floor(now.getTime() / 1000),
            },
            includeForEmbedding: false,
          },
        },
      };
      const uploadUrl = await presignPut({
        bucket: cfg.docsBucket,
        key,
        contentType: upload.contentType,
        expiresIn: EXPIRY_SECONDS,
      });
      await dynamoClient.send(
        new PutCommand({
          TableName: cfg.tableName,
          Item: {
            pk: documentPk(documentId),
            sk: 'META',
            gsi1pk: 'ORG#DOCUMENT',
            gsi1sk: uploadedAt,
            documentId,
            uploaderSub: auth.sub,
            title: upload.safeName,
            s3Key: key,
            contentType: upload.contentType,
            sizeBytes: upload.sizeBytes,
            status: 'UPLOADING',
            uploadedAt,
            updatedAt: uploadedAt,
          },
          ConditionExpression: 'attribute_not_exists(pk)',
        }),
      );
      await s3Client.send(
        new PutObjectCommand({
          Bucket: cfg.docsBucket,
          Key: `${key}.metadata.json`,
          ContentType: 'application/json',
          Body: JSON.stringify(sidecar),
        }),
      );
      const response: UploadResponse = {
        documentId,
        uploadUrl,
        key,
        expiresIn: EXPIRY_SECONDS,
      };
      return jsonResponse(201, response);
    } catch (error) {
      return errorResponse(error);
    } finally {
      log('info', 'presign request completed', {
        durationMs: Date.now() - started,
      });
    }
  });
}

export const handler = handlePresign;
