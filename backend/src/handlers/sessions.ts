import { QueryCommand } from '@aws-sdk/lib-dynamodb';
import type {
  APIGatewayProxyEventV2WithJWTAuthorizer,
  APIGatewayProxyStructuredResultV2,
  Context,
} from 'aws-lambda';
import type { Page, SessionDetail, SessionSummary } from '@kb/shared';

import { dynamoClient } from '../lib/clients';
import { loadSessionsConfig } from '../lib/config';
import {
  decodePageToken,
  encodePageToken,
  getSessionMeta,
  sessionGsiPk,
  sessionPk,
  toMessageView,
  type MessageItem,
  type SessionMetaItem,
} from '../lib/ddb';
import {
  ForbiddenError,
  NotFoundError,
  ValidationError,
} from '../lib/errors';
import {
  authFromHttpApi,
  errorResponse,
  jsonResponse,
} from '../lib/http';
import { log, setLogUser, withLogContext } from '../lib/logger';

const cfg = loadSessionsConfig();
const LIST_CURSOR_KEYS = ['pk', 'sk', 'gsi1pk', 'gsi1sk'] as const;
const MESSAGE_CURSOR_KEYS = ['pk', 'sk'] as const;

function parseLimit(raw: string | undefined, fallback: number): number {
  if (raw === undefined) return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value <= 0 || value > 100) {
    throw new ValidationError('limit must be between 1 and 100');
  }
  return value;
}

function summary(item: SessionMetaItem): SessionSummary {
  return {
    sessionId: item.sessionId,
    title: item.title,
    createdAt: item.createdAt,
    updatedAt: item.updatedAt,
    messageCount: item.messageCount,
  };
}

async function listSessions(
  event: APIGatewayProxyEventV2WithJWTAuthorizer,
  userSub: string,
): Promise<APIGatewayProxyStructuredResultV2> {
  const partition = sessionGsiPk(userSub);
  const token = event.queryStringParameters?.nextToken;
  const exclusiveStartKey =
    token === undefined
      ? undefined
      : decodePageToken(token, {
          expectedKeys: LIST_CURSOR_KEYS,
          partition: { key: 'gsi1pk', value: partition },
        });
  const output = await dynamoClient.send(
    new QueryCommand({
      TableName: cfg.tableName,
      IndexName: 'gsi1',
      KeyConditionExpression: 'gsi1pk = :partition',
      ExpressionAttributeValues: { ':partition': partition },
      ScanIndexForward: false,
      Limit: parseLimit(event.queryStringParameters?.limit, 25),
      ...(exclusiveStartKey === undefined
        ? {}
        : { ExclusiveStartKey: exclusiveStartKey }),
    }),
  );
  const response: Page<SessionSummary> = {
    items: ((output.Items ?? []) as SessionMetaItem[]).map(summary),
    ...(output.LastEvaluatedKey === undefined
      ? {}
      : {
          nextToken: encodePageToken(
            output.LastEvaluatedKey as Record<string, string>,
          ),
        }),
  };
  return jsonResponse(200, response);
}

async function sessionDetail(
  event: APIGatewayProxyEventV2WithJWTAuthorizer,
  userSub: string,
): Promise<APIGatewayProxyStructuredResultV2> {
  const sessionId = event.pathParameters?.sessionId;
  if (sessionId === undefined || sessionId === '') {
    throw new ValidationError('sessionId is required');
  }
  const meta = await getSessionMeta({ tableName: cfg.tableName, sessionId });
  if (meta === undefined) {
    throw new NotFoundError('Session not found');
  }
  if (meta.userSub !== userSub) {
    throw new ForbiddenError();
  }

  const partition = sessionPk(sessionId);
  const token = event.queryStringParameters?.nextToken;
  const exclusiveStartKey =
    token === undefined
      ? undefined
      : decodePageToken(token, {
          expectedKeys: MESSAGE_CURSOR_KEYS,
          partition: { key: 'pk', value: partition },
        });
  const output = await dynamoClient.send(
    new QueryCommand({
      TableName: cfg.tableName,
      KeyConditionExpression: 'pk = :pk AND begins_with(sk, :message)',
      ExpressionAttributeValues: {
        ':pk': partition,
        ':message': 'MSG#',
      },
      ScanIndexForward: false,
      Limit: parseLimit(event.queryStringParameters?.limit, 50),
      ...(exclusiveStartKey === undefined
        ? {}
        : { ExclusiveStartKey: exclusiveStartKey }),
    }),
  );
  const detail: SessionDetail = {
    ...summary(meta),
    messages: ((output.Items ?? []) as MessageItem[])
      .reverse()
      .map(toMessageView),
    ...(output.LastEvaluatedKey === undefined
      ? {}
      : {
          nextToken: encodePageToken(
            output.LastEvaluatedKey as Record<string, string>,
          ),
        }),
  };
  return jsonResponse(200, detail);
}

export async function handleSessions(
  event: APIGatewayProxyEventV2WithJWTAuthorizer,
  context: Context,
): Promise<APIGatewayProxyStructuredResultV2> {
  return withLogContext({ requestId: context.awsRequestId }, async () => {
    const started = Date.now();
    log('info', 'sessions request started');
    try {
      const auth = authFromHttpApi(event);
      setLogUser(auth.sub);
      if (event.routeKey === 'GET /v1/sessions') {
        return await listSessions(event, auth.sub);
      }
      if (event.routeKey === 'GET /v1/sessions/{sessionId}') {
        return await sessionDetail(event, auth.sub);
      }
      throw new NotFoundError();
    } catch (error) {
      return errorResponse(error);
    } finally {
      log('info', 'sessions request completed', {
        durationMs: Date.now() - started,
      });
    }
  });
}

export const handler = handleSessions;
