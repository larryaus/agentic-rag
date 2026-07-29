import { DynamoDBDocumentClient, GetCommand } from '@aws-sdk/lib-dynamodb';
import type { Context } from 'aws-lambda';
import { mockClient } from 'aws-sdk-client-mock';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const presignGet = vi.hoisted(() => vi.fn());
vi.mock('../lib/presigner', () => ({
  presignGet,
  presignPut: vi.fn(),
}));

import { handleDocuments } from '../handlers/documents';
import { jwtEvent } from './test-utils';

const ddb = mockClient(DynamoDBDocumentClient);
const context = { awsRequestId: 'request' } as Context;

function event(documentId: string) {
  return jwtEvent({
    routeKey: 'GET /v1/documents/{documentId}/download',
    rawPath: `/v1/documents/${documentId}/download`,
    method: 'GET',
    sub: 'user',
    pathParameters: { documentId },
  });
}

describe('document download', () => {
  beforeEach(() => {
    ddb.reset();
    presignGet.mockReset().mockResolvedValue('https://signed.example/get');
  });

  it('returns 404 for an unknown document without signing', async () => {
    ddb.on(GetCommand).resolves({});
    const response = await handleDocuments(event('unknown'), context);
    expect(response.statusCode).toBe(404);
    expect(presignGet).not.toHaveBeenCalled();
  });

  it('returns 409 until the document is READY', async () => {
    ddb.on(GetCommand).resolves({
      Item: { status: 'INGESTING', s3Key: 'uploads/key' },
    });
    const response = await handleDocuments(event('pending'), context);
    expect(response.statusCode).toBe(409);
    expect(presignGet).not.toHaveBeenCalled();
  });

  it('uses the mocked presigner boundary for a READY document', async () => {
    ddb.on(GetCommand).resolves({
      Item: { status: 'READY', s3Key: 'uploads/user/id/doc.md' },
    });
    const response = await handleDocuments(event('ready'), context);
    expect(response.statusCode).toBe(200);
    expect(presignGet).toHaveBeenCalledWith({
      bucket: 'test-documents',
      key: 'uploads/user/id/doc.md',
      expiresIn: 300,
    });
  });
});
