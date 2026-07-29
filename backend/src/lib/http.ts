import type {
  APIGatewayProxyEventV2WithJWTAuthorizer,
  APIGatewayProxyStructuredResultV2,
} from 'aws-lambda';

import type { AuthContext } from './auth';
import { AppError, UnauthorizedError, ValidationError } from './errors';

export function authFromHttpApi(
  event: APIGatewayProxyEventV2WithJWTAuthorizer,
): AuthContext {
  const claims = event.requestContext.authorizer.jwt.claims;
  const sub = claims.sub;
  if (typeof sub !== 'string' || sub === '') {
    throw new UnauthorizedError();
  }
  return {
    sub,
    username: typeof claims.username === 'string' ? claims.username : sub,
  };
}

export function parseJsonBody(event: {
  body?: string | null;
  isBase64Encoded?: boolean;
}): unknown {
  if (event.body === undefined || event.body === null) {
    throw new ValidationError('Request body is required');
  }
  try {
    const body = event.isBase64Encoded
      ? Buffer.from(event.body, 'base64').toString('utf8')
      : event.body;
    return JSON.parse(body) as unknown;
  } catch {
    throw new ValidationError('Request body must be valid JSON');
  }
}

export function jsonResponse(
  statusCode: number,
  body: unknown,
): APIGatewayProxyStructuredResultV2 {
  return {
    statusCode,
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  };
}

export function errorResponse(error: unknown): APIGatewayProxyStructuredResultV2 {
  if (error instanceof AppError) {
    return jsonResponse(error.statusCode, { message: error.message });
  }
  return jsonResponse(500, { message: 'Internal server error' });
}
