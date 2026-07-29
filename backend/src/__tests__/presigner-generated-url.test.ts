import { afterEach, describe, expect, it, vi } from 'vitest';

import { s3Client } from '../lib/clients';
import { presignPut } from '../lib/presigner';

describe('presignPut generated URL', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('signs content-type without payload checksum parameters', async () => {
    vi.spyOn(s3Client.config, 'credentials').mockResolvedValue({
      accessKeyId: 'test-access-key',
      secretAccessKey: 'test-secret-key',
    });

    const uploadUrl = await presignPut({
      bucket: 'documents',
      key: 'uploads/user/document/file.md',
      contentType: 'text/markdown',
      expiresIn: 300,
    });
    const parameters = [...new URL(uploadUrl).searchParams.entries()];
    const checksumParameters = parameters.filter(([name]) => {
      const lowerName = name.toLowerCase();
      return (
        lowerName.startsWith('x-amz-checksum-') ||
        lowerName === 'x-amz-sdk-checksum-algorithm'
      );
    });
    const signedHeaders = parameters.find(
      ([name]) => name.toLowerCase() === 'x-amz-signedheaders',
    )?.[1];

    expect(
      await s3Client.config.requestChecksumCalculation(),
    ).toBe('WHEN_REQUIRED');
    expect(checksumParameters).toEqual([]);
    expect(signedHeaders?.split(';')).toEqual(
      expect.arrayContaining(['content-type', 'host', 'if-none-match']),
    );
  });
});
