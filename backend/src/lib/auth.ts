/**
 * The Function URL verifies access tokens in-process; HTTP API handlers trust its authorizer.
 */
import { CognitoJwtVerifier } from 'aws-jwt-verify';

import { loadChatConfig } from './config';
import { UnauthorizedError } from './errors';

// Stage 2 department filtering will resolve department from a user-profile item in
// DynamoDB or a Cognito GetUser call, not from the access token.
export type AuthContext = { sub: string; username: string };

const cfg = loadChatConfig();
const verifier = CognitoJwtVerifier.create({
  userPoolId: cfg.userPoolId,
  tokenUse: 'access',
  clientId: cfg.userPoolClientId,
});

export async function verifyBearer(
  header: string | undefined,
): Promise<AuthContext> {
  if (header === undefined || !header.startsWith('Bearer ')) {
    throw new UnauthorizedError();
  }
  const token = header.slice('Bearer '.length).trim();
  if (token === '') {
    throw new UnauthorizedError();
  }
  try {
    const payload = await verifier.verify(token);
    return {
      sub: payload.sub,
      username:
        typeof payload.username === 'string' ? payload.username : payload.sub,
    };
  } catch {
    throw new UnauthorizedError();
  }
}
