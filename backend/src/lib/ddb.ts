/**
 * Key builders and strict cursor validation centralize the application's tenancy boundary.
 */
import type { Message } from '@aws-sdk/client-bedrock-runtime';
import {
  GetCommand,
  PutCommand,
  QueryCommand,
  TransactWriteCommand,
} from '@aws-sdk/lib-dynamodb';
import type { Citation, MessageView } from '@kb/shared';
import { randomUUID } from 'node:crypto';

import { CITATION_RE } from './citations';
import { dynamoClient } from './clients';
import { ValidationError } from './errors';

export type SessionMetaItem = {
  pk: string;
  sk: 'META';
  gsi1pk: string;
  gsi1sk: string;
  sessionId: string;
  userSub: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  messageCount: number;
  ttl: number;
};

export type MessageItem = {
  pk: string;
  sk: string;
  userSub: string;
  role: 'user' | 'assistant';
  content: string;
  citations: Citation[];
  usage?: { inputTokens: number; outputTokens: number };
  stopReason?: string;
  createdAt: string;
  ttl: number;
};

export type DocumentItem = {
  pk: string;
  sk: 'META';
  gsi1pk: 'ORG#DOCUMENT';
  gsi1sk: string;
  documentId: string;
  uploaderSub: string;
  title: string;
  s3Key: string;
  contentType: string;
  sizeBytes: number;
  status: 'UPLOADING' | 'PENDING' | 'INGESTING' | 'READY' | 'FAILED';
  ingestionJobId?: string;
  errorMessage?: string;
  uploadedAt: string;
  updatedAt: string;
};

export const sessionPk = (sessionId: string): string => `SESSION#${sessionId}`;
export const documentPk = (documentId: string): string => `DOC#${documentId}`;
export const sessionGsiPk = (userSub: string): string =>
  `USER#${userSub}#SESSION`;

export function messageSk(createdAt: string, id: string = randomUUID()): string {
  return `MSG#${createdAt}#${id}`;
}

export function encodePageToken(key: Record<string, string>): string {
  return Buffer.from(JSON.stringify(key), 'utf8').toString('base64url');
}

export function decodePageToken(
  token: string,
  opts: {
    expectedKeys: readonly string[];
    partition: { key: string; value: string };
  },
): Record<string, string> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(Buffer.from(token, 'base64url').toString('utf8')) as unknown;
  } catch {
    throw new ValidationError('Malformed nextToken');
  }
  if (
    typeof parsed !== 'object' ||
    parsed === null ||
    Array.isArray(parsed)
  ) {
    throw new ValidationError('Malformed nextToken');
  }
  const record = parsed as Record<string, unknown>;
  const actualKeys = Object.keys(record).sort();
  const expectedKeys = [...opts.expectedKeys].sort();
  if (
    actualKeys.length !== expectedKeys.length ||
    actualKeys.some((key, index) => key !== expectedKeys[index])
  ) {
    throw new ValidationError('Malformed nextToken');
  }
  if (Object.values(record).some((value) => typeof value !== 'string')) {
    throw new ValidationError('Malformed nextToken');
  }
  if (record[opts.partition.key] !== opts.partition.value) {
    throw new ValidationError('Malformed nextToken');
  }
  return record as Record<string, string>;
}

export async function getSessionMeta(opts: {
  tableName: string;
  sessionId: string;
}): Promise<SessionMetaItem | undefined> {
  const output = await dynamoClient.send(
    new GetCommand({
      TableName: opts.tableName,
      Key: { pk: sessionPk(opts.sessionId), sk: 'META' },
      ConsistentRead: true,
    }),
  );
  return output.Item as SessionMetaItem | undefined;
}

export async function getDocument(opts: {
  tableName: string;
  documentId: string;
}): Promise<DocumentItem | undefined> {
  const output = await dynamoClient.send(
    new GetCommand({
      TableName: opts.tableName,
      Key: { pk: documentPk(opts.documentId), sk: 'META' },
      ConsistentRead: true,
    }),
  );
  return output.Item as DocumentItem | undefined;
}

export async function loadRecentHistory(opts: {
  tableName: string;
  sessionId: string;
  limit: number;
}): Promise<Message[]> {
  const output = await dynamoClient.send(
    new QueryCommand({
      TableName: opts.tableName,
      KeyConditionExpression: 'pk = :pk AND begins_with(sk, :message)',
      ExpressionAttributeValues: {
        ':pk': sessionPk(opts.sessionId),
        ':message': 'MSG#',
      },
      ScanIndexForward: false,
      Limit: opts.limit,
    }),
  );
  // DynamoDB returns newest-first; Bedrock history must be chronological.
  return ((output.Items ?? []) as MessageItem[])
    .reverse()
    .map((item) => ({
      role: item.role,
      content: [
        {
          text:
            item.role === 'assistant'
              ? item.content.replace(CITATION_RE, '')
              : item.content,
        },
      ],
    }));
}

export function makeMessageItem(opts: {
  sessionId: string;
  userSub: string;
  role: 'user' | 'assistant';
  content: string;
  citations?: Citation[];
  usage?: { inputTokens: number; outputTokens: number };
  stopReason?: string;
  createdAt: string;
  ttl: number;
  id?: string;
}): MessageItem {
  return {
    pk: sessionPk(opts.sessionId),
    sk: messageSk(opts.createdAt, opts.id),
    userSub: opts.userSub,
    role: opts.role,
    content: opts.content,
    citations: opts.citations ?? [],
    ...(opts.usage === undefined ? {} : { usage: opts.usage }),
    ...(opts.stopReason === undefined ? {} : { stopReason: opts.stopReason }),
    createdAt: opts.createdAt,
    ttl: opts.ttl,
  };
}

export async function persistSubmittedMessage(opts: {
  tableName: string;
  item: MessageItem;
}): Promise<void> {
  await dynamoClient.send(
    new TransactWriteCommand({
      TransactItems: [
        {
          Put: {
            TableName: opts.tableName,
            Item: opts.item,
            ConditionExpression:
              'attribute_not_exists(pk) AND attribute_not_exists(sk)',
          },
        },
        {
          Update: {
            TableName: opts.tableName,
            Key: { pk: opts.item.pk, sk: 'META' },
            UpdateExpression:
              'SET #updatedAt = :updatedAt, #gsi1sk = :gsi1sk, #ttl = :ttl ADD messageCount :one',
            ConditionExpression: '#userSub = :userSub',
            ExpressionAttributeNames: {
              '#updatedAt': 'updatedAt',
              '#gsi1sk': 'gsi1sk',
              '#ttl': 'ttl',
              '#userSub': 'userSub',
            },
            ExpressionAttributeValues: {
              ':updatedAt': opts.item.createdAt,
              ':gsi1sk': opts.item.createdAt,
              ':ttl': opts.item.ttl,
              ':one': 1,
              ':userSub': opts.item.userSub,
            },
          },
        },
      ],
    }),
  );
}

export async function createSessionMeta(opts: {
  tableName: string;
  sessionId: string;
  userSub: string;
  title: string;
  createdAt: string;
  ttl: number;
}): Promise<void> {
  await dynamoClient.send(
    new PutCommand({
      TableName: opts.tableName,
      Item: {
        pk: sessionPk(opts.sessionId),
        sk: 'META',
        gsi1pk: sessionGsiPk(opts.userSub),
        gsi1sk: opts.createdAt,
        sessionId: opts.sessionId,
        userSub: opts.userSub,
        title: opts.title,
        createdAt: opts.createdAt,
        updatedAt: opts.createdAt,
        messageCount: 0,
        ttl: opts.ttl,
      } satisfies SessionMetaItem,
      ConditionExpression: 'attribute_not_exists(pk)',
    }),
  );
}

export async function persistCompletedTurn(opts: {
  tableName: string;
  sessionId: string;
  userSub: string;
  updatedAt: string;
  ttl: number;
  assistantItem: MessageItem;
}): Promise<void> {
  const metaKey = { pk: sessionPk(opts.sessionId), sk: 'META' };

  await dynamoClient.send(
    new TransactWriteCommand({
      TransactItems: [
        {
          Put: {
            TableName: opts.tableName,
            Item: opts.assistantItem,
            ConditionExpression:
              'attribute_not_exists(pk) AND attribute_not_exists(sk)',
          },
        },
        {
          Update: {
            TableName: opts.tableName,
            Key: metaKey,
            UpdateExpression:
              'SET #updatedAt = :updatedAt, #gsi1sk = :gsi1sk, #ttl = :ttl ADD messageCount :one',
            ConditionExpression: '#userSub = :userSub',
            ExpressionAttributeNames: {
              '#updatedAt': 'updatedAt',
              '#gsi1sk': 'gsi1sk',
              '#ttl': 'ttl',
              '#userSub': 'userSub',
            },
            ExpressionAttributeValues: {
              ':updatedAt': opts.updatedAt,
              ':gsi1sk': opts.updatedAt,
              ':ttl': opts.ttl,
              ':one': 1,
              ':userSub': opts.userSub,
            },
          },
        },
      ],
    }),
  );
}

export function toMessageView(item: MessageItem): MessageView {
  return {
    role: item.role,
    content: item.content,
    citations: item.citations ?? [],
    createdAt: item.createdAt,
  };
}
