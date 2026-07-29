import type { APIGatewayProxyEventV2WithJWTAuthorizer } from 'aws-lambda';

type JwtClaim = string | number | boolean | string[];

export function jwtEvent(options: {
  routeKey: string;
  rawPath: string;
  method: string;
  sub: string;
  body?: string;
  claims?: Record<string, JwtClaim>;
  pathParameters?: Record<string, string>;
}): APIGatewayProxyEventV2WithJWTAuthorizer {
  const claims = { sub: options.sub, ...options.claims };
  return {
    version: '2.0',
    routeKey: options.routeKey,
    rawPath: options.rawPath,
    rawQueryString: '',
    headers: {
      authorization: 'Bearer test-token',
      'content-type': 'application/json',
    },
    requestContext: {
      accountId: '123456789012',
      apiId: 'test-api',
      authorizer: {
        principalId: options.sub,
        integrationLatency: 0,
        jwt: { claims, scopes: ['kb-api/access'] },
      },
      domainName: 'test-api.execute-api.us-east-1.amazonaws.com',
      domainPrefix: 'test-api',
      http: {
        method: options.method,
        path: options.rawPath,
        protocol: 'HTTP/1.1',
        sourceIp: '127.0.0.1',
        userAgent: 'vitest',
      },
      requestId: 'test-request',
      routeKey: options.routeKey,
      stage: '$default',
      time: '01/Jan/2026:00:00:00 +0000',
      timeEpoch: 1_767_225_600_000,
    },
    isBase64Encoded: false,
    ...(options.body === undefined ? {} : { body: options.body }),
    ...(options.pathParameters === undefined
      ? {}
      : { pathParameters: options.pathParameters }),
  };
}
