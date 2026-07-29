/**
 * Async-local context keeps structured request fields isolated across concurrent invocations.
 */
import { AsyncLocalStorage } from 'node:async_hooks';

type LogLevel = 'debug' | 'info' | 'warn' | 'error';
type LogFields = Record<string, unknown>;
type LogContext = { requestId?: string; userSub?: string };

const context = new AsyncLocalStorage<LogContext>();

export function withLogContext<T>(
  fields: LogContext,
  callback: () => Promise<T>,
): Promise<T> {
  return context.run(fields, callback);
}

export function setLogUser(userSub: string): void {
  const current = context.getStore();
  if (current !== undefined) {
    current.userSub = userSub;
  }
}

export function log(level: LogLevel, msg: string, fields: LogFields = {}): void {
  const current = context.getStore();
  console.log(
    JSON.stringify({
      level,
      msg,
      ...(current?.requestId === undefined ? {} : { requestId: current.requestId }),
      ...(current?.userSub === undefined ? {} : { userSub: current.userSub }),
      ...fields,
    }),
  );
}
