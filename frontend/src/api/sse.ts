import type { SseEvent } from '@kb/shared';

const KNOWN_EVENTS = new Set([
  'session',
  'tool_use',
  'citation',
  'text',
  'done',
  'error',
]);

function parseFrame(frame: string): SseEvent | undefined {
  let eventName = '';
  const dataLines: string[] = [];
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) {
      eventName = line.slice('event:'.length).trim();
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice('data:'.length).trimStart());
    }
  }
  if (!KNOWN_EVENTS.has(eventName) || dataLines.length === 0) {
    return undefined;
  }
  try {
    const data = JSON.parse(dataLines.join('\n')) as unknown;
    if (typeof data !== 'object' || data === null || Array.isArray(data)) {
      return undefined;
    }
    return { type: eventName, ...data } as SseEvent;
  } catch {
    return undefined;
  }
}

function drainFrames(
  buffer: string,
  onEvent: (event: SseEvent) => void,
): string {
  let remaining = buffer;
  let boundary = remaining.indexOf('\n\n');
  while (boundary >= 0) {
    const frame = remaining.slice(0, boundary);
    remaining = remaining.slice(boundary + 2);
    const event = parseFrame(frame);
    if (event !== undefined) onEvent(event);
    boundary = remaining.indexOf('\n\n');
  }
  return remaining;
}

export async function consumeSse(
  response: Response,
  onEvent: (event: SseEvent) => void,
): Promise<void> {
  if (response.body === null) {
    throw new Error('Streaming response has no body');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    buffer = drainFrames(buffer, onEvent);
  }
  buffer += decoder.decode();
  drainFrames(buffer, onEvent);
  // A trailing unterminated frame is deliberately discarded.
}

export async function streamChat(opts: {
  url: string;
  accessToken: string;
  sessionId?: string;
  message: string;
  onEvent: (event: SseEvent) => void;
}): Promise<void> {
  const response = await fetch(opts.url, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${opts.accessToken}`,
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      message: opts.message,
      ...(opts.sessionId === undefined ? {} : { sessionId: opts.sessionId }),
    }),
  });
  if (!response.ok) {
    throw new Error(`Chat request failed (${response.status})`);
  }
  await consumeSse(response, opts.onEvent);
}
