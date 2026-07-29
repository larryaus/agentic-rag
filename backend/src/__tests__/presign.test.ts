import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';
import { DynamoDBDocumentClient, PutCommand } from '@aws-sdk/lib-dynamodb';
import type { Context } from 'aws-lambda';
import { mockClient } from 'aws-sdk-client-mock';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const presignPut = vi.hoisted(() => vi.fn());
vi.mock('../lib/presigner', () => ({
  presignPut,
  presignGet: vi.fn(),
}));

import { handlePresign, sanitizeFilename } from '../handlers/presign';
import { jwtEvent } from './test-utils';

const s3 = mockClient(S3Client);
const ddb = mockClient(DynamoDBDocumentClient);
const context = { awsRequestId: 'request' } as Context;

function uploadEvent(body: object) {
  return jwtEvent({
    routeKey: 'POST /v1/uploads',
    rawPath: '/v1/uploads',
    method: 'POST',
    sub: 'user-123',
    body: JSON.stringify(body),
    claims: { username: 'larry' },
  });
}

describe('presign handler', () => {
  beforeEach(() => {
    s3.reset();
    ddb.reset();
    presignPut.mockReset().mockResolvedValue('https://signed.example/put');
    s3.on(PutObjectCommand).resolves({});
    ddb.on(PutCommand).resolves({});
  });

  it('rejects disallowed types, mismatched extensions, and oversize files', async () => {
    const disallowed = await handlePresign(
      uploadEvent({
        filename: 'malware.exe',
        contentType: 'application/octet-stream',
        sizeBytes: 10,
      }),
      context,
    );
    const mismatch = await handlePresign(
      uploadEvent({
        filename: 'malware.exe',
        contentType: 'text/plain',
        sizeBytes: 10,
      }),
      context,
    );
    const oversize = await handlePresign(
      uploadEvent({
        filename: 'large.pdf',
        contentType: 'application/pdf',
        sizeBytes: 26_214_401,
      }),
      context,
    );
    expect([disallowed.statusCode, mismatch.statusCode, oversize.statusCode]).toEqual([
      400, 400, 400,
    ]);
    expect(presignPut).not.toHaveBeenCalled();
  });

  it('sanitizes path traversal and hostile unicode into safe names', () => {
    expect(sanitizeFilename('../../etc/passwd.txt')).toBe('passwd.txt');
    expect(sanitizeFilename('恶意📦report---.md')).toBe('report-.md');
    expect(sanitizeFilename('恶意📦')).toBe('');
  });

  it('rejects a filename that sanitizes empty or collides with sidecars', async () => {
    const empty = await handlePresign(
      uploadEvent({
        filename: '恶意📦',
        contentType: 'text/plain',
        sizeBytes: 1,
      }),
      context,
    );
    const sidecar = await handlePresign(
      uploadEvent({
        filename: 'foo.metadata.json',
        contentType: 'text/plain',
        sizeBytes: 1,
      }),
      context,
    );
    expect(empty.statusCode).toBe(400);
    expect(sidecar.statusCode).toBe(400);
  });

  it('writes an UPLOADING record and the exact metadata sidecar', async () => {
    const response = await handlePresign(
      uploadEvent({
        filename: '../../notes.md',
        contentType: 'text/markdown',
        sizeBytes: 123,
      }),
      context,
    );

    expect(response.statusCode).toBe(201);
    const put = ddb.commandCalls(PutCommand)[0]?.args[0].input;
    expect(put?.Item).toEqual(
      expect.objectContaining({
        gsi1pk: 'ORG#DOCUMENT',
        uploaderSub: 'user-123',
        title: 'notes.md',
        status: 'UPLOADING',
      }),
    );
    const sidecarPut = s3.commandCalls(PutObjectCommand)[0]?.args[0].input;
    expect(sidecarPut?.Key).toMatch(/notes\.md\.metadata\.json$/);
    const sidecar = JSON.parse(String(sidecarPut?.Body)) as {
      metadataAttributes: Record<
        string,
        { value: Record<string, unknown>; includeForEmbedding: boolean }
      >;
    };
    expect(sidecar.metadataAttributes.documentId?.value.type).toBe('STRING');
    expect(sidecar.metadataAttributes.uploaderSub?.value.stringValue).toBe(
      'user-123',
    );
    expect(
      sidecar.metadataAttributes.uploadedAt?.value.numberValue,
    ).toEqual(expect.any(Number));
    expect(presignPut).toHaveBeenCalledWith(
      expect.objectContaining({
        contentType: 'text/markdown',
        expiresIn: 300,
      }),
    );
  });

  it('keeps the UPLOADING row when the later sidecar write fails', async () => {
    let rowExistedBeforeSidecar = false;
    s3.on(PutObjectCommand).callsFake(() => {
      rowExistedBeforeSidecar = ddb.commandCalls(PutCommand).length === 1;
      throw new Error('temporary sidecar failure');
    });

    const response = await handlePresign(
      uploadEvent({
        filename: 'notes.md',
        contentType: 'text/markdown',
        sizeBytes: 123,
      }),
      context,
    );

    expect(response.statusCode).toBe(500);
    expect(rowExistedBeforeSidecar).toBe(true);
    expect(ddb.commandCalls(PutCommand)).toHaveLength(1);
    expect(ddb.commandCalls(PutCommand)[0]?.args[0].input.Item).toEqual(
      expect.objectContaining({ status: 'UPLOADING' }),
    );
    expect(s3.commandCalls(PutObjectCommand)).toHaveLength(1);
  });
});
