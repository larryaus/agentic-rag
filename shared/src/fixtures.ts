import type { DocumentStatus, DocumentSummary, MessageView, Page, SessionDetail, SessionSummary, UploadRequest, UploadResponse } from './api';
import type { Citation, SseEvent, Usage } from './sse';

const sessionId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const documentId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const timestamp = '2026-01-01T00:00:00.123Z';
const citation = { ref: 1, title: '日本語.md', documentId, score: 0.875, snippet: 'café' } satisfies Citation;
const usage = { inputTokens: 100, outputTokens: 20 } satisfies Usage;
const session = { sessionId, title: 'First question', createdAt: timestamp, updatedAt: timestamp, messageCount: 2 } satisfies SessionSummary;
const statuses = ['UPLOADING', 'PENDING', 'INGESTING', 'READY', 'FAILED'] satisfies DocumentStatus[];
const documents = statuses.map((status) => ({ documentId, title: 'Guide.md', contentType: 'text/markdown', sizeBytes: 123, status, uploadedAt: timestamp })) satisfies DocumentSummary[];
const messages = [
  { role: 'user', content: 'Question', citations: [], createdAt: timestamp },
  { role: 'assistant', content: 'Answer [ref:1]', citations: [citation], createdAt: timestamp },
] satisfies MessageView[];

export const fixtures = {
  documentStatuses: statuses,
  documentSummaries: documents,
  sessionSummaries: [session] satisfies SessionSummary[],
  messageViews: messages,
  sessionDetails: [{ ...session, messages }, { ...session, messages, nextToken: 'opaque-token' }] satisfies SessionDetail[],
  documentPages: [{ items: documents }, { items: [], nextToken: 'opaque-token' }] satisfies Page<DocumentSummary>[],
  sessionPages: [{ items: [session] }, { items: [], nextToken: 'opaque-token' }] satisfies Page<SessionSummary>[],
  uploadRequests: [{ filename: 'Guide.md', contentType: 'text/markdown', sizeBytes: 123 }] satisfies UploadRequest[],
  uploadResponses: [{ documentId, uploadUrl: 'https://signed.example/put', key: `uploads/owner/${documentId}/Guide.md`, expiresIn: 300 }] satisfies UploadResponse[],
  citations: [citation] satisfies Citation[],
  usages: [usage] satisfies Usage[],
  sseEvents: [
    { type: 'session', sessionId },
    { type: 'tool_use', name: 'search_knowledge_base', input: {} },
    { type: 'tool_use', name: 'search_knowledge_base', input: { query: '日本語 café', score: 1.0, tiny: 1e-7, large: 1e21 } },
    { type: 'citation', ...citation },
    { type: 'citation', ref: 2, title: 'one', documentId, score: 1.0, snippet: '' },
    { type: 'text', delta: 'Hello\n世界 😀 "\\' },
    { type: 'done', sessionId, stopReason: 'end_turn', usage },
    { type: 'error', message: 'Please try again.' },
  ] satisfies SseEvent[],
};
