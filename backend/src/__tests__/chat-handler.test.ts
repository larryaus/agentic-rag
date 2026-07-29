import type { APIGatewayProxyEventV2, Context } from 'aws-lambda';
import { Writable } from 'node:stream';
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  verifyBearer: vi.fn(),
  createSessionMeta: vi.fn(),
  getSessionMeta: vi.fn(),
  loadRecentHistory: vi.fn(),
  makeMessageItem: vi.fn((value: object) => value),
  persistCompletedTurn: vi.fn(),
  persistSubmittedMessage: vi.fn(),
  runAgent: vi.fn(),
}));

vi.mock('../lib/auth', () => ({ verifyBearer: mocks.verifyBearer }));
vi.mock('../lib/agent', () => ({ runAgent: mocks.runAgent }));
vi.mock('../lib/ddb', () => ({
  createSessionMeta: mocks.createSessionMeta,
  getSessionMeta: mocks.getSessionMeta,
  loadRecentHistory: mocks.loadRecentHistory,
  makeMessageItem: mocks.makeMessageItem,
  persistCompletedTurn: mocks.persistCompletedTurn,
  persistSubmittedMessage: mocks.persistSubmittedMessage,
}));

type ResponseMetadata = {
  statusCode: number;
  headers: Record<string, string>;
};

class CaptureStream extends Writable {
  public readonly chunks: string[] = [];

  public override _write(
    chunk: Buffer,
    _encoding: BufferEncoding,
    callback: (error?: Error | null) => void,
  ): void {
    this.chunks.push(chunk.toString('utf8'));
    callback();
  }
}

const metadata: ResponseMetadata[] = [];

beforeAll(() => {
  Object.defineProperty(globalThis, 'awslambda', {
    configurable: true,
    value: {
      streamifyResponse: (callback: unknown) => callback,
      HttpResponseStream: {
        from: (
          stream: awslambda.HttpResponseStream,
          responseMetadata: ResponseMetadata,
        ) => {
          metadata.push(responseMetadata);
          return stream;
        },
      },
    },
  });
});

function event(opts: {
  authorization?: string;
  body?: object;
}): APIGatewayProxyEventV2 {
  return {
    version: '2.0',
    routeKey: '$default',
    rawPath: '/',
    rawQueryString: '',
    headers:
      opts.authorization === undefined
        ? {}
        : { authorization: opts.authorization },
    requestContext: {
      accountId: 'test',
      apiId: 'test',
      domainName: 'test',
      domainPrefix: 'test',
      http: {
        method: 'POST',
        path: '/',
        protocol: 'HTTP/1.1',
        sourceIp: '127.0.0.1',
        userAgent: 'vitest',
      },
      requestId: 'request',
      routeKey: '$default',
      stage: '$default',
      time: '',
      timeEpoch: 0,
    },
    isBase64Encoded: false,
    ...(opts.body === undefined ? {} : { body: JSON.stringify(opts.body) }),
  };
}

const context = {
  awsRequestId: 'request-id',
} as Context;

describe('chat handler response ordering', () => {
  beforeEach(() => {
    metadata.length = 0;
    Object.values(mocks).forEach((mock) => mock.mockReset());
    mocks.makeMessageItem.mockImplementation((value: object) => value);
    mocks.loadRecentHistory.mockResolvedValue([]);
    mocks.createSessionMeta.mockResolvedValue(undefined);
    mocks.persistSubmittedMessage.mockResolvedValue(undefined);
    mocks.persistCompletedTurn.mockResolvedValue(undefined);
  });

  it('writes and ends a plain 401 before any SSE frame', async () => {
    mocks.verifyBearer.mockRejectedValue(new Error('invalid'));
    const { chatHandler } = await import('../handlers/chat');
    const stream = new CaptureStream();

    await chatHandler(
      event({ authorization: 'Bearer bad', body: { message: 'hello' } }),
      stream as unknown as awslambda.HttpResponseStream,
      context,
    );

    expect(metadata).toEqual([
      {
        statusCode: 401,
        headers: { 'content-type': 'application/json' },
      },
    ]);
    expect(stream.writableEnded).toBe(true);
    expect(stream.chunks.join('')).toContain('"Unauthorized"');
    expect(stream.chunks.join('')).not.toContain('event:');
    expect(mocks.runAgent).not.toHaveBeenCalled();
  });

  it('writes session first and done last for a valid request', async () => {
    mocks.verifyBearer.mockResolvedValue({ sub: 'owner', username: 'owner' });
    mocks.runAgent.mockImplementation(
      async (options: { emit: (eventValue: object) => void }) => {
        options.emit({ type: 'text', delta: 'hello' });
        return {
          text: 'hello',
          citations: [],
          usage: { inputTokens: 1, outputTokens: 1 },
          stopReason: 'end_turn',
        };
      },
    );
    const { chatHandler } = await import('../handlers/chat');
    const stream = new CaptureStream();

    await chatHandler(
      event({ authorization: 'Bearer good', body: { message: 'hello' } }),
      stream as unknown as awslambda.HttpResponseStream,
      context,
    );

    const output = stream.chunks.join('');
    expect(metadata[0]?.statusCode).toBe(200);
    expect(output.indexOf('event: session')).toBe(0);
    expect(output.lastIndexOf('event: done')).toBeGreaterThan(
      output.indexOf('event: text'),
    );
    expect(stream.writableEnded).toBe(true);
    expect(mocks.createSessionMeta.mock.invocationCallOrder[0]).toBeLessThan(
      mocks.persistSubmittedMessage.mock.invocationCallOrder[0] ??
        Number.MAX_SAFE_INTEGER,
    );
    expect(mocks.persistSubmittedMessage.mock.invocationCallOrder[0]).toBeLessThan(
      mocks.runAgent.mock.invocationCallOrder[0] ?? Number.MAX_SAFE_INTEGER,
    );
  });

  it('keeps a new session readable after a generic agent failure', async () => {
    mocks.verifyBearer.mockResolvedValue({ sub: 'owner', username: 'owner' });
    mocks.runAgent.mockRejectedValue(new Error('throttled upstream'));
    const { chatHandler } = await import('../handlers/chat');
    const stream = new CaptureStream();

    await chatHandler(
      event({ authorization: 'Bearer good', body: { message: 'hello' } }),
      stream as unknown as awslambda.HttpResponseStream,
      context,
    );

    expect(mocks.createSessionMeta).toHaveBeenCalledOnce();
    expect(mocks.createSessionMeta).toHaveBeenCalledWith(
      expect.objectContaining({
        tableName: 'test-table',
        userSub: 'owner',
        title: 'hello',
      }),
    );
    expect(mocks.persistCompletedTurn).toHaveBeenCalledOnce();
    expect(mocks.persistCompletedTurn).toHaveBeenCalledWith(
      expect.objectContaining({
        assistantItem: expect.objectContaining({
          role: 'assistant',
          content: 'The request could not be completed. Please try again.',
          stopReason: 'error',
        }),
      }),
    );
    expect(mocks.createSessionMeta.mock.invocationCallOrder[0]).toBeLessThan(
      mocks.persistSubmittedMessage.mock.invocationCallOrder[0] ??
        Number.MAX_SAFE_INTEGER,
    );
    expect(stream.chunks.join('')).toContain('event: error');
    expect(stream.chunks.join('')).not.toContain('throttled upstream');
    expect(stream.writableEnded).toBe(true);
  });

  it('returns 403 for another user before committing the stream', async () => {
    const sessionId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
    mocks.verifyBearer.mockResolvedValue({ sub: 'caller', username: 'caller' });
    mocks.getSessionMeta.mockResolvedValue({
      userSub: 'another-user',
    });
    const { chatHandler } = await import('../handlers/chat');
    const stream = new CaptureStream();

    await chatHandler(
      event({
        authorization: 'Bearer good',
        body: { sessionId, message: 'hello' },
      }),
      stream as unknown as awslambda.HttpResponseStream,
      context,
    );

    expect(metadata[0]?.statusCode).toBe(403);
    expect(stream.chunks.join('')).not.toContain('event:');
    expect(stream.writableEnded).toBe(true);
    expect(mocks.loadRecentHistory).not.toHaveBeenCalled();
    expect(mocks.runAgent).not.toHaveBeenCalled();
  });
});
