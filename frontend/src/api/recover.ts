/**
 * Recovers a chat turn whose stream dropped, by reading the conversation back.
 *
 * The server finishes and saves a turn even after the client disconnects, which is
 * what happens when iOS suspends an installed app mid-answer. Once the server has sent
 * the session event, the question is saved and the answer will be too, so the client
 * polls the conversation until the answer appears.
 */
import type { MessageView, SessionDetail } from '@kb/shared';

import type { AppConfig } from '../config';
import { apiFetch } from './http';

export type RecoveredTurn =
  | { state: 'answered'; messages: MessageView[] }
  /** The question never reached storage; `messages` is what the server holds. */
  | { state: 'lost'; messages: MessageView[] }
  | { state: 'timeout' };

const POLL_MS = 3000;

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason);
      return;
    }
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = (): void => {
      clearTimeout(timer);
      reject(signal.reason);
    };
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

// A suspended app cannot make requests, so polling waits until it is in front again.
function whenVisible(signal: AbortSignal): Promise<void> {
  if (document.visibilityState !== 'hidden') return Promise.resolve();
  return new Promise((resolve, reject) => {
    const done = (): void => {
      document.removeEventListener('visibilitychange', onChange);
      signal.removeEventListener('abort', onAbort);
    };
    const onChange = (): void => {
      if (document.visibilityState === 'hidden') return;
      done();
      resolve();
    };
    const onAbort = (): void => {
      done();
      reject(signal.reason);
    };
    document.addEventListener('visibilitychange', onChange);
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

export async function recoverTurn(opts: {
  config: AppConfig;
  sessionId: string;
  question: string;
  /** Epoch milliseconds after which a still-unanswered turn is reported as a timeout. */
  deadline: number;
  signal: AbortSignal;
  onPending: () => void;
  wait?: (ms: number, signal: AbortSignal) => Promise<void>;
}): Promise<RecoveredTurn> {
  const wait = opts.wait ?? sleep;
  let missingOnce = false;
  for (let attempt = 0; ; attempt += 1) {
    if (attempt > 0) await wait(POLL_MS, opts.signal);
    await whenVisible(opts.signal);
    let messages: MessageView[];
    try {
      const response = await apiFetch(
        opts.config,
        `/v1/sessions/${encodeURIComponent(opts.sessionId)}`,
        { signal: opts.signal },
      );
      messages = ((await response.json()) as SessionDetail).messages;
    } catch (cause) {
      if (opts.signal.aborted) throw cause;
      // Still offline, or a transient failure: keep trying until the deadline.
      if (Date.now() > opts.deadline) return { state: 'timeout' };
      continue;
    }
    const last = messages.at(-1);
    const previous = messages.at(-2);
    if (last?.role === 'user' && last.content.trim() === opts.question) {
      if (Date.now() > opts.deadline) return { state: 'timeout' };
      opts.onPending();
      continue;
    }
    if (
      last?.role === 'assistant' &&
      previous?.role === 'user' &&
      previous.content.trim() === opts.question
    ) {
      return { state: 'answered', messages };
    }
    // The question is saved just after the session event, so look once more before
    // concluding it was never stored.
    if (missingOnce || Date.now() > opts.deadline) {
      return { state: 'lost', messages };
    }
    missingOnce = true;
  }
}
