import { ConditionalCheckFailedException } from '@aws-sdk/client-dynamodb';
import { DeleteObjectCommand, S3Client } from '@aws-sdk/client-s3';
import {
  DeleteCommand,
  DynamoDBDocumentClient,
  GetCommand,
} from '@aws-sdk/lib-dynamodb';
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
const s3 = mockClient(S3Client);
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

function removeEvent(documentId: string) {
  return jwtEvent({
    routeKey: 'DELETE /v1/documents/{documentId}',
    rawPath: `/v1/documents/${documentId}`,
    method: 'DELETE',
    sub: 'user',
    pathParameters: { documentId },
  });
}

describe('failed document removal', () => {
  beforeEach(() => {
    ddb.reset();
    s3.reset();
    ddb.on(DeleteCommand).resolves({});
    s3.on(DeleteObjectCommand).resolves({});
  });

  it('removes a FAILED document record and its stored objects', async () => {
    ddb.on(GetCommand).resolves({
      Item: { status: 'FAILED', s3Key: 'uploads/user/id/doc.md' },
    });

    const response = await handleDocuments(removeEvent('id'), context);

    expect(response.statusCode).toBe(204);
    const deletes = ddb.commandCalls(DeleteCommand);
    expect(deletes).toHaveLength(1);
    expect(deletes[0]?.args[0].input).toMatchObject({
      TableName: 'test-table',
      Key: { pk: 'DOC#id', sk: 'META' },
      ExpressionAttributeValues: { ':failed': 'FAILED' },
    });
    expect(
      s3
        .commandCalls(DeleteObjectCommand)
        .map((call) => call.args[0].input.Key)
        .sort(),
    ).toEqual([
      'uploads/user/id/doc.md',
      'uploads/user/id/doc.md.metadata.json',
    ]);
  });

  it('returns 404 for an unknown document without deleting anything', async () => {
    ddb.on(GetCommand).resolves({});

    const response = await handleDocuments(removeEvent('unknown'), context);

    expect(response.statusCode).toBe(404);
    expect(ddb.commandCalls(DeleteCommand)).toHaveLength(0);
    expect(s3.commandCalls(DeleteObjectCommand)).toHaveLength(0);
  });

  it.each(['UPLOADING', 'PENDING', 'INGESTING', 'READY'])(
    'refuses to remove a %s document',
    async (status) => {
      ddb.on(GetCommand).resolves({
        Item: { status, s3Key: 'uploads/user/id/doc.md' },
      });

      const response = await handleDocuments(removeEvent('id'), context);

      expect(response.statusCode).toBe(409);
      expect(ddb.commandCalls(DeleteCommand)).toHaveLength(0);
      expect(s3.commandCalls(DeleteObjectCommand)).toHaveLength(0);
    },
  );

  it('returns 409 and keeps the objects when the status changes mid-request', async () => {
    ddb.on(GetCommand).resolves({
      Item: { status: 'FAILED', s3Key: 'uploads/user/id/doc.md' },
    });
    ddb.on(DeleteCommand).rejects(
      new ConditionalCheckFailedException({ message: 'changed', $metadata: {} }),
    );

    const response = await handleDocuments(removeEvent('id'), context);

    expect(response.statusCode).toBe(409);
    expect(s3.commandCalls(DeleteObjectCommand)).toHaveLength(0);
  });

  it('still succeeds when a stored object cannot be deleted', async () => {
    ddb.on(GetCommand).resolves({
      Item: { status: 'FAILED', s3Key: 'uploads/user/id/doc.md' },
    });
    s3.on(DeleteObjectCommand).rejects(new Error('s3 unavailable'));

    const response = await handleDocuments(removeEvent('id'), context);

    expect(response.statusCode).toBe(204);
  });
});
