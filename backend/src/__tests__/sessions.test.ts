import {
  DynamoDBDocumentClient,
  GetCommand,
  QueryCommand,
} from '@aws-sdk/lib-dynamodb';
import type { Context } from 'aws-lambda';
import { mockClient } from 'aws-sdk-client-mock';
import { expect, it } from 'vitest';

import { handleSessions } from '../handlers/sessions';
import { jwtEvent } from './test-utils';

const ddb = mockClient(DynamoDBDocumentClient);

it('returns 403 before querying messages owned by another user', async () => {
  ddb.reset();
  ddb.on(GetCommand).resolves({
    Item: {
      pk: 'SESSION#secret',
      sk: 'META',
      sessionId: 'secret',
      userSub: 'owner',
    },
  });
  const event = jwtEvent({
    routeKey: 'GET /v1/sessions/{sessionId}',
    rawPath: '/v1/sessions/secret',
    method: 'GET',
    sub: 'attacker',
    pathParameters: { sessionId: 'secret' },
  });

  const response = await handleSessions(
    event,
    { awsRequestId: 'request' } as Context,
  );

  expect(response.statusCode).toBe(403);
  expect(ddb.commandCalls(QueryCommand)).toHaveLength(0);
});
