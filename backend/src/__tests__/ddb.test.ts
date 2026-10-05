import {
  DynamoDBDocumentClient,
  PutCommand,
  QueryCommand,
  TransactWriteCommand,
} from '@aws-sdk/lib-dynamodb';
import { mockClient } from 'aws-sdk-client-mock';
import { describe, expect, it } from 'vitest';

import {
  createSessionMeta,
  decodePageToken,
  documentPk,
  encodePageToken,
  loadRecentHistory,
  makeMessageItem,
  type MessageItem,
  persistCompletedTurn,
  persistSubmittedMessage,
  sessionGsiPk,
  sessionPk,
  toConverseHistory,
} from '../lib/ddb';
import { ValidationError } from '../lib/errors';

const ddb = mockClient(DynamoDBDocumentClient);

describe('DynamoDB model', () => {
  it('uses entity-typed keys and numeric TTL message items', () => {
    expect(sessionPk('abc')).toBe('SESSION#abc');
    expect(documentPk('abc')).toBe('DOC#abc');
    expect(sessionGsiPk('user')).toBe('USER#user#SESSION');
    expect(
      makeMessageItem({
        sessionId: 'session',
        userSub: 'user',
        role: 'user',
        content: 'hello',
        createdAt: '2026-01-01T00:00:00.000Z',
        ttl: 123,
        id: 'message',
      }),
    ).toEqual(
      expect.objectContaining({
        pk: 'SESSION#session',
        sk: 'MSG#2026-01-01T00:00:00.000Z#message',
        userSub: 'user',
        ttl: 123,
      }),
    );
  });

  it('round-trips and validates a partition-bound pagination token', () => {
    const key = {
      pk: 'DOC#1',
      sk: 'META',
      gsi1pk: 'ORG#DOCUMENT',
      gsi1sk: '2026-01-01T00:00:00.000Z',
    };
    const token = encodePageToken(key);
    expect(
      decodePageToken(token, {
        expectedKeys: ['pk', 'sk', 'gsi1pk', 'gsi1sk'],
        partition: { key: 'gsi1pk', value: 'ORG#DOCUMENT' },
      }),
    ).toEqual(key);
    expect(() =>
      decodePageToken(token, {
        expectedKeys: ['pk', 'sk', 'gsi1pk', 'gsi1sk'],
        partition: { key: 'gsi1pk', value: 'USER#attacker#SESSION' },
      }),
    ).toThrow(ValidationError);
  });

  it('loads bounded chronological history and strips stale assistant refs', async () => {
    ddb.reset();
    ddb.on(QueryCommand).resolves({
      Items: [
        { role: 'assistant', content: 'second answer [ref:2]', citations: [] },
        { role: 'user', content: 'second question', citations: [] },
        { role: 'assistant', content: 'first answer [ref:1]', citations: [] },
        { role: 'user', content: 'first question', citations: [] },
      ],
    });

    const history = await loadRecentHistory({
      tableName: 'table',
      sessionId: 'session',
      limit: 20,
    });

    expect(history).toEqual([
      { role: 'user', content: [{ text: 'first question' }] },
      { role: 'assistant', content: [{ text: 'first answer' }] },
      { role: 'user', content: [{ text: 'second question' }] },
      { role: 'assistant', content: [{ text: 'second answer' }] },
    ]);
    expect(ddb.commandCalls(QueryCommand)[0]?.args[0].input).toEqual(
      expect.objectContaining({
        ScanIndexForward: false,
        Limit: 20,
      }),
    );
  });

  it('keeps only complete pairs so Converse accepts the history', () => {
    const item = (
      role: 'user' | 'assistant',
      content: string,
    ): MessageItem =>
      makeMessageItem({
        sessionId: 'session',
        userSub: 'user',
        role,
        content,
        createdAt: '2026-01-01T00:00:00.000Z',
        ttl: 123,
      });

    expect(
      toConverseHistory([
        // The window opened on an answer whose question fell outside it.
        item('assistant', 'orphaned answer'),
        // A turn that timed out before its answer was stored.
        item('user', 'unanswered question'),
        item('user', 'answered question'),
        item('assistant', 'answer [ref:1]'),
        // A stream that failed before any text was produced.
        item('user', 'question with blank answer'),
        item('assistant', '  '),
        // The answer was a citation marker and nothing else.
        item('user', 'question with marker-only answer'),
        item('assistant', '[ref:3]'),
        // A trailing question the caller would follow with another user turn.
        item('user', 'trailing question'),
      ]),
    ).toEqual([
      { role: 'user', content: [{ text: 'answered question' }] },
      { role: 'assistant', content: [{ text: 'answer' }] },
    ]);
  });

  it('creates session metadata conditionally before messages can be written', async () => {
    ddb.reset();
    ddb.on(PutCommand).resolves({});

    await createSessionMeta({
      tableName: 'table',
      sessionId: 'session',
      userSub: 'user',
      title: 'First question',
      createdAt: '2026-01-01T00:00:00.000Z',
      ttl: 123,
    });

    expect(ddb.commandCalls(PutCommand)[0]?.args[0].input).toEqual(
      expect.objectContaining({
        ConditionExpression: 'attribute_not_exists(pk)',
        Item: expect.objectContaining({
          pk: 'SESSION#session',
          sk: 'META',
          userSub: 'user',
          messageCount: 0,
        }),
      }),
    );
  });

  it('atomically counts a submitted user message even if the turn stops there', async () => {
    ddb.reset();
    ddb.on(TransactWriteCommand).resolves({});
    const userItem = makeMessageItem({
      sessionId: 'session',
      userSub: 'user',
      role: 'user',
      content: 'hello',
      createdAt: '2026-01-01T00:00:00.000Z',
      ttl: 123,
      id: 'user-message',
    });

    await persistSubmittedMessage({ tableName: 'table', item: userItem });

    const transaction =
      ddb.commandCalls(TransactWriteCommand)[0]?.args[0].input.TransactItems;
    expect(transaction).toHaveLength(2);
    expect(transaction?.[0]?.Put).toEqual(
      expect.objectContaining({
        Item: userItem,
        ConditionExpression:
          'attribute_not_exists(pk) AND attribute_not_exists(sk)',
      }),
    );
    expect(transaction?.[1]?.Update).toEqual(
      expect.objectContaining({
        Key: { pk: 'SESSION#session', sk: 'META' },
        UpdateExpression: expect.stringContaining('ADD messageCount :one'),
        ExpressionAttributeValues: expect.objectContaining({ ':one': 1 }),
      }),
    );
  });

  it('counts only the assistant message when completing a submitted turn', async () => {
    ddb.reset();
    ddb.on(TransactWriteCommand).resolves({});
    const assistantItem = makeMessageItem({
      sessionId: 'session',
      userSub: 'user',
      role: 'assistant',
      content: 'answer',
      createdAt: '2026-01-01T00:00:01.000Z',
      ttl: 123,
      id: 'assistant-message',
    });

    await persistCompletedTurn({
      tableName: 'table',
      sessionId: 'session',
      userSub: 'user',
      updatedAt: assistantItem.createdAt,
      ttl: assistantItem.ttl,
      assistantItem,
    });

    const transaction =
      ddb.commandCalls(TransactWriteCommand)[0]?.args[0].input.TransactItems;
    expect(transaction).toHaveLength(2);
    expect(transaction?.[0]?.Put?.Item).toEqual(assistantItem);
    expect(transaction?.[1]?.Update).toEqual(
      expect.objectContaining({
        UpdateExpression: expect.stringContaining('ADD messageCount :one'),
        ExpressionAttributeValues: expect.objectContaining({ ':one': 1 }),
      }),
    );
  });
});
