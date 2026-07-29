import type { SseEvent } from '@kb/shared';

export function encodeSse(event: SseEvent): string {
  switch (event.type) {
    case 'session':
      return frame(event.type, { sessionId: event.sessionId });
    case 'tool_use':
      return frame(event.type, { name: event.name, input: event.input });
    case 'citation':
      return frame(event.type, {
        ref: event.ref,
        title: event.title,
        documentId: event.documentId,
        score: event.score,
        snippet: event.snippet,
      });
    case 'text':
      return frame(event.type, { delta: event.delta });
    case 'done':
      return frame(event.type, {
        sessionId: event.sessionId,
        stopReason: event.stopReason,
        usage: event.usage,
      });
    case 'error':
      return frame(event.type, { message: event.message });
  }
}

function frame(name: string, data: unknown): string {
  return `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;
}
