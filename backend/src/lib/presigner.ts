/**
 * Signing is wrapped because credential resolution occurs outside the S3 client's send path.
 */
import { GetObjectCommand, PutObjectCommand } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';

import { s3Client } from './clients';

export async function presignPut(opts: {
  bucket: string;
  key: string;
  contentType: string;
  expiresIn: number;
}): Promise<string> {
  return getSignedUrl(
    s3Client,
    new PutObjectCommand({
      Bucket: opts.bucket,
      Key: opts.key,
      ContentType: opts.contentType,
      IfNoneMatch: '*',
    }),
    {
      expiresIn: opts.expiresIn,
      signableHeaders: new Set(['content-type']),
    },
  );
}

export async function presignGet(opts: {
  bucket: string;
  key: string;
  expiresIn: number;
}): Promise<string> {
  return getSignedUrl(
    s3Client,
    new GetObjectCommand({ Bucket: opts.bucket, Key: opts.key }),
    { expiresIn: opts.expiresIn },
  );
}
