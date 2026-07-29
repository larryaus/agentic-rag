import { beforeEach, describe, expect, it, vi } from 'vitest';

const verify = vi.hoisted(() => vi.fn());

vi.mock('aws-jwt-verify', () => ({
  CognitoJwtVerifier: {
    create: () => ({ verify }),
  },
}));

import { verifyBearer } from '../lib/auth';
import { UnauthorizedError } from '../lib/errors';

describe('verifyBearer', () => {
  beforeEach(() => verify.mockReset());

  it('rejects missing and malformed authorization headers', async () => {
    await expect(verifyBearer(undefined)).rejects.toBeInstanceOf(
      UnauthorizedError,
    );
    await expect(verifyBearer('Basic token')).rejects.toBeInstanceOf(
      UnauthorizedError,
    );
    expect(verify).not.toHaveBeenCalled();
  });

  it('maps verifier rejection to UnauthorizedError', async () => {
    verify.mockImplementationOnce(async () => {
      throw new Error('bad signature');
    });
    await expect(verifyBearer('Bearer invalid')).rejects.toBeInstanceOf(
      UnauthorizedError,
    );
  });

  it('returns a shared auth context for a valid access token', async () => {
    verify.mockResolvedValue({ sub: 'user-1', username: 'larry' });
    await expect(verifyBearer('Bearer valid')).resolves.toEqual({
      sub: 'user-1',
      username: 'larry',
    });
  });
});
