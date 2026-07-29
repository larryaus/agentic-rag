/**
 * Retrieval is intentionally separate from generation so the agent can decide when to search.
 */
import { RetrieveCommand } from '@aws-sdk/client-bedrock-agent-runtime';

import { bedrockAgentRuntimeClient } from './clients';
import { loadChatConfig } from './config';

export type RawChunk = {
  text: string;
  sourceUri: string;
  documentId: string;
  title: string;
  score: number;
};

const UUID_V4 =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function titleFromUri(sourceUri: string): string {
  const basename = sourceUri.split('/').at(-1) ?? '';
  try {
    return decodeURIComponent(basename);
  } catch {
    return basename;
  }
}

export async function retrieve(opts: {
  query: string;
  topK: number;
}): Promise<RawChunk[]> {
  const cfg = loadChatConfig();
  const response = await bedrockAgentRuntimeClient.send(
    new RetrieveCommand({
      knowledgeBaseId: cfg.knowledgeBaseId,
      retrievalQuery: { text: opts.query },
      retrievalConfiguration: {
        vectorSearchConfiguration: {
          numberOfResults: opts.topK,
          // Stage 2: retrievalConfiguration.vectorSearchConfiguration.filter goes here (department/date).
        },
      },
    }),
  );

  return (response.retrievalResults ?? []).map((result) => {
    const sourceUri = result.location?.s3Location?.uri ?? '';
    const metadataDocumentId = result.metadata?.documentId;
    const documentId =
      typeof metadataDocumentId === 'string' &&
      UUID_V4.test(metadataDocumentId)
        ? metadataDocumentId
        : '';
    return {
      text: result.content?.text ?? '',
      sourceUri,
      documentId,
      title: titleFromUri(sourceUri),
      score: result.score ?? 0,
    };
  });
}
