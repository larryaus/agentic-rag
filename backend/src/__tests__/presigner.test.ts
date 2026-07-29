import { PutObjectCommand } from '@aws-sdk/client-s3';
import { describe, expect, it, vi } from 'vitest';

const getSignedUrl = vi.hoisted(() => vi.fn());

vi.mock('@aws-sdk/s3-request-presigner', () => ({ getSignedUrl }));

import { presignPut } from '../lib/presigner';

describe('presignPut', () => {
  it('makes upload URLs conditional so they cannot overwrite the object', async () => {
    getSignedUrl.mockResolvedValue('https://signed.example/put');
    const result = await presignPut({
      bucket: 'documents',
      key: 'uploads/user/document/file.md',
      contentType: 'text/markdown',
      expiresIn: 300,
    });

    expect(result).toBe('https://signed.example/put');
    const command = getSignedUrl.mock.calls[0]?.[1];
    expect(command).toBeInstanceOf(PutObjectCommand);
    expect((command as PutObjectCommand).input).toEqual(
      expect.objectContaining({
        Bucket: 'documents',
        Key: 'uploads/user/document/file.md',
        ContentType: 'text/markdown',
        IfNoneMatch: '*',
      }),
    );
    expect(getSignedUrl.mock.calls[0]?.[2]).toEqual(
      expect.objectContaining({
        expiresIn: 300,
        signableHeaders: new Set(['content-type']),
      }),
    );
  });
});
