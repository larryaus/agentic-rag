import type { Citation } from './sse';

export type DocumentStatus =
  | 'UPLOADING'
  | 'PENDING'
  | 'INGESTING'
  | 'READY'
  | 'FAILED';

export type DocumentSummary = {
  documentId: string;
  title: string;
  contentType: string;
  sizeBytes: number;
  status: DocumentStatus;
  uploadedAt: string;
};

export type SessionSummary = {
  sessionId: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  messageCount: number;
};

export type MessageView = {
  role: 'user' | 'assistant';
  content: string;
  citations: Citation[];
  createdAt: string;
};

export type SessionDetail = SessionSummary & {
  messages: MessageView[];
  nextToken?: string;
};

export type Page<T> = {
  items: T[];
  nextToken?: string;
};

export type UploadRequest = {
  filename: string;
  contentType: string;
  sizeBytes: number;
};

export type UploadResponse = {
  documentId: string;
  uploadUrl: string;
  key: string;
  expiresIn: number;
};
