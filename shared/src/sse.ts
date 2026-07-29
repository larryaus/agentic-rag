export type Citation = {
  ref: number;
  title: string;
  documentId: string;
  score: number;
  snippet: string;
};

export type Usage = {
  inputTokens: number;
  outputTokens: number;
};

export type SseEvent =
  | { type: 'session'; sessionId: string }
  | { type: 'tool_use'; name: string; input: Record<string, unknown> }
  | ({ type: 'citation' } & Citation)
  | { type: 'text'; delta: string }
  | { type: 'done'; sessionId: string; stopReason: string; usage: Usage }
  | { type: 'error'; message: string };
