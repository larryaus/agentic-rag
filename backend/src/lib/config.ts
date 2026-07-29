/**
 * Handler-specific readers keep each Lambda's environment and validation surface minimal.
 */

function required(name: string): string {
  const value = process.env[name];
  if (value === undefined || value.trim() === '') {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

function positiveInteger(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined) {
    return fallback;
  }
  const value = Number(raw);
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error(`${name} must be a positive integer`);
  }
  return value;
}

export type ChatConfig = {
  tableName: string;
  knowledgeBaseId: string;
  chatModelId: string;
  userPoolId: string;
  userPoolClientId: string;
  retrievalTopK: number;
  maxToolIterations: number;
  maxHistoryMessages: number;
  sessionTtlDays: number;
};

export function loadChatConfig(): ChatConfig {
  return {
    tableName: required('TABLE_NAME'),
    knowledgeBaseId: required('KNOWLEDGE_BASE_ID'),
    chatModelId: required('CHAT_MODEL_ID'),
    userPoolId: required('USER_POOL_ID'),
    userPoolClientId: required('USER_POOL_CLIENT_ID'),
    retrievalTopK: positiveInteger('RETRIEVAL_TOP_K', 8),
    maxToolIterations: positiveInteger('MAX_TOOL_ITERATIONS', 6),
    maxHistoryMessages: positiveInteger('MAX_HISTORY_MESSAGES', 20),
    sessionTtlDays: positiveInteger('SESSION_TTL_DAYS', 90),
  };
}

export type IngestConfig = {
  tableName: string;
  knowledgeBaseId: string;
  dataSourceId: string;
  docsBucket: string;
  maxUploadBytes: number;
};

export function loadIngestConfig(): IngestConfig {
  return {
    tableName: required('TABLE_NAME'),
    knowledgeBaseId: required('KNOWLEDGE_BASE_ID'),
    dataSourceId: required('DATA_SOURCE_ID'),
    docsBucket: required('DOCS_BUCKET'),
    maxUploadBytes: positiveInteger('MAX_UPLOAD_BYTES', 26_214_400),
  };
}

export type ReconcilerConfig = {
  tableName: string;
  knowledgeBaseId: string;
  dataSourceId: string;
  docsBucket: string;
  abandonedUploadMinutes: number;
};

export function loadReconcilerConfig(): ReconcilerConfig {
  return {
    tableName: required('TABLE_NAME'),
    knowledgeBaseId: required('KNOWLEDGE_BASE_ID'),
    dataSourceId: required('DATA_SOURCE_ID'),
    docsBucket: required('DOCS_BUCKET'),
    abandonedUploadMinutes: positiveInteger('ABANDONED_UPLOAD_MINUTES', 10),
  };
}

export type PresignConfig = {
  tableName: string;
  docsBucket: string;
  maxUploadBytes: number;
};

export function loadPresignConfig(): PresignConfig {
  return {
    tableName: required('TABLE_NAME'),
    docsBucket: required('DOCS_BUCKET'),
    maxUploadBytes: positiveInteger('MAX_UPLOAD_BYTES', 26_214_400),
  };
}

export type DocumentsConfig = {
  tableName: string;
  docsBucket: string;
};

export function loadDocumentsConfig(): DocumentsConfig {
  return {
    tableName: required('TABLE_NAME'),
    docsBucket: required('DOCS_BUCKET'),
  };
}

export type SessionsConfig = {
  tableName: string;
};

export function loadSessionsConfig(): SessionsConfig {
  return { tableName: required('TABLE_NAME') };
}
