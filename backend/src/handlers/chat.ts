import type {
  APIGatewayProxyEventV2,
  Context,
} from 'aws-lambda';
import type { SseEvent } from '@kb/shared';
import { randomUUID } from 'node:crypto';
import { finished } from 'node:stream/promises';

import { runAgent } from '../lib/agent';
import { verifyBearer, type AuthContext } from '../lib/auth';
import { loadChatConfig } from '../lib/config';
import {
  createSessionMeta,
  getSessionMeta,
  loadRecentHistory,
  makeMessageItem,
  persistCompletedTurn,
  persistSubmittedMessage,
  type SessionMetaItem,
} from '../lib/ddb';
import {
  AppError,
  BedrockStreamError,
  ForbiddenError,
  NotFoundError,
  UnauthorizedError,
  ValidationError,
} from '../lib/errors';
import { log, setLogUser, withLogContext } from '../lib/logger';
import { encodeSse } from '../lib/sse';

const cfg = loadChatConfig();
const UUID_V4 =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

type ChatBody = { sessionId?: string; message: string };

function parseChatBody(event: APIGatewayProxyEventV2): ChatBody {
  if (event.body === undefined || event.body === null) {
    throw new ValidationError('Request body is required');
  }
  let parsed: unknown;
  try {
    const raw = event.isBase64Encoded
      ? Buffer.from(event.body, 'base64').toString('utf8')
      : event.body;
    parsed = JSON.parse(raw) as unknown;
  } catch {
    throw new ValidationError('Request body must be valid JSON');
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new ValidationError();
  }
  const body = parsed as Record<string, unknown>;
  if (
    typeof body.message !== 'string' ||
    body.message.trim() === '' ||
    body.message.length > 8000
  ) {
    throw new ValidationError(
      'message must be non-empty and no more than 8000 characters',
    );
  }
  if (
    body.sessionId !== undefined &&
    (typeof body.sessionId !== 'string' || !UUID_V4.test(body.sessionId))
  ) {
    throw new ValidationError('sessionId must be a UUID v4');
  }
  return {
    message: body.message.trim(),
    ...(body.sessionId === undefined
      ? {}
      : { sessionId: body.sessionId as string }),
  };
}

export async function failFast(
  responseStream: awslambda.HttpResponseStream,
  statusCode: number,
  message: string,
): Promise<void> {
  const stream = awslambda.HttpResponseStream.from(responseStream, {
    statusCode,
    headers: { 'content-type': 'application/json' },
  });
  stream.write(JSON.stringify({ message }));
  stream.end();
  await finished(stream);
}

function statusMessage(error: unknown): {
  statusCode: number;
  message: string;
} {
  if (error instanceof AppError) {
    return { statusCode: error.statusCode, message: error.message };
  }
  return { statusCode: 500, message: 'Internal server error' };
}

function safeStreamingMessage(error: unknown): string {
  if (error instanceof BedrockStreamError) {
    return 'The model response stream ended unexpectedly. Please try again.';
  }
  return 'The request could not be completed. Please try again.';
}

export async function chatHandler(
  event: APIGatewayProxyEventV2,
  responseStream: awslambda.HttpResponseStream,
  context: Context,
): Promise<void> {
  await withLogContext({ requestId: context.awsRequestId }, async () => {
    const started = Date.now();
    log('info', 'chat request started');

    if (event.requestContext.http.method !== 'POST') {
      await failFast(responseStream, 405, 'Method not allowed');
      log('info', 'chat request completed', {
        durationMs: Date.now() - started,
      });
      return;
    }

    let auth: AuthContext;
    try {
      auth = await verifyBearer(
        event.headers.authorization ?? event.headers.Authorization,
      );
      setLogUser(auth.sub);
    } catch {
      await failFast(responseStream, 401, new UnauthorizedError().message);
      log('info', 'chat request completed', {
        durationMs: Date.now() - started,
      });
      return;
    }

    let body: ChatBody;
    try {
      body = parseChatBody(event);
    } catch (error) {
      const failure = statusMessage(error);
      await failFast(responseStream, failure.statusCode, failure.message);
      log('info', 'chat request completed', {
        durationMs: Date.now() - started,
      });
      return;
    }

    const isNewSession = body.sessionId === undefined;
    const sessionId = body.sessionId ?? randomUUID();
    if (!isNewSession) {
      try {
        const existingMeta: SessionMetaItem | undefined = await getSessionMeta({
          tableName: cfg.tableName,
          sessionId,
        });
        if (existingMeta === undefined) {
          throw new NotFoundError('Session not found');
        }
        if (existingMeta.userSub !== auth.sub) {
          throw new ForbiddenError();
        }
      } catch (error) {
        const failure = statusMessage(error);
        await failFast(responseStream, failure.statusCode, failure.message);
        log('info', 'chat request completed', {
          durationMs: Date.now() - started,
        });
        return;
      }
    }

    const nowMs = Date.now();
    const createdAt = new Date(nowMs).toISOString();
    const ttl = Math.floor(
      (nowMs + cfg.sessionTtlDays * 24 * 60 * 60 * 1000) / 1000,
    );
    if (isNewSession) {
      try {
        await createSessionMeta({
          tableName: cfg.tableName,
          sessionId,
          userSub: auth.sub,
          title: body.message.replace(/\s+/g, ' ').slice(0, 60),
          createdAt,
          ttl,
        });
      } catch (error) {
        log('error', 'failed to create session metadata', {
          errorType: error instanceof Error ? error.name : 'UnknownError',
        });
        const failure = statusMessage(error);
        await failFast(responseStream, failure.statusCode, failure.message);
        log('info', 'chat request completed', {
          durationMs: Date.now() - started,
        });
        return;
      }
    }

    const stream = awslambda.HttpResponseStream.from(responseStream, {
      statusCode: 200,
      headers: {
        'content-type': 'text/event-stream',
        'cache-control': 'no-cache',
      },
    });
    const emit = (sseEvent: SseEvent): void => {
      stream.write(encodeSse(sseEvent));
    };

    try {
      emit({ type: 'session', sessionId });
      const history = isNewSession
        ? []
        : await loadRecentHistory({
            tableName: cfg.tableName,
            sessionId,
            limit: cfg.maxHistoryMessages,
          });
      const userItem = makeMessageItem({
        sessionId,
        userSub: auth.sub,
        role: 'user',
        content: body.message,
        createdAt,
        ttl,
      });
      // The submitted turn is durable before Bedrock runs, so a timeout cannot lose it.
      await persistSubmittedMessage({ tableName: cfg.tableName, item: userItem });

      try {
        const result = await runAgent({
          history,
          userMessage: body.message,
          emit,
          topK: cfg.retrievalTopK,
          maxIterations: cfg.maxToolIterations,
          modelId: cfg.chatModelId,
        });
        const updatedAt = new Date(Math.max(Date.now(), nowMs + 1)).toISOString();
        const assistantItem = makeMessageItem({
          sessionId,
          userSub: auth.sub,
          role: 'assistant',
          content: result.text,
          citations: result.citations,
          usage: result.usage,
          stopReason: result.stopReason,
          createdAt: updatedAt,
          ttl,
        });
        await persistCompletedTurn({
          tableName: cfg.tableName,
          sessionId,
          userSub: auth.sub,
          updatedAt,
          ttl,
          assistantItem,
        });
        emit({
          type: 'done',
          sessionId,
          stopReason: result.stopReason,
          usage: result.usage,
        });
      } catch (error) {
        const updatedAt = new Date(
          Math.max(Date.now(), nowMs + 1),
        ).toISOString();
        const failureResult =
          error instanceof BedrockStreamError
            ? error.partialResult
            : {
                text: safeStreamingMessage(error),
                citations: [],
                usage: { inputTokens: 0, outputTokens: 0 },
                stopReason: 'error',
              };
        const assistantItem = makeMessageItem({
          sessionId,
          userSub: auth.sub,
          role: 'assistant',
          content: failureResult.text,
          citations: failureResult.citations,
          usage: failureResult.usage,
          stopReason: failureResult.stopReason,
          createdAt: updatedAt,
          ttl,
        });
        await persistCompletedTurn({
          tableName: cfg.tableName,
          sessionId,
          userSub: auth.sub,
          updatedAt,
          ttl,
          assistantItem,
        });
        throw error;
      }
    } catch (error) {
      log('error', 'chat request failed after stream commit', {
        errorType: error instanceof Error ? error.name : 'UnknownError',
      });
      emit({ type: 'error', message: safeStreamingMessage(error) });
    } finally {
      stream.end();
      await finished(stream);
      log('info', 'chat request completed', {
        durationMs: Date.now() - started,
      });
    }
  });
}

export const handler = awslambda.streamifyResponse(chatHandler);
