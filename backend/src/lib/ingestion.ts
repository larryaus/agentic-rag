import { IngestKnowledgeBaseDocumentsCommand } from '@aws-sdk/client-bedrock-agent';

import { bedrockAgentClient } from './clients';

export async function ingestDocument(opts: {
  knowledgeBaseId: string;
  dataSourceId: string;
  docsBucket: string;
  documentId: string;
  key: string;
}): Promise<void> {
  const uri = `s3://${opts.docsBucket}/${opts.key}`;
  await bedrockAgentClient.send(
    new IngestKnowledgeBaseDocumentsCommand({
      knowledgeBaseId: opts.knowledgeBaseId,
      dataSourceId: opts.dataSourceId,
      // Event retries and scheduled recovery must identify the same request.
      clientToken: `ingest-${opts.documentId}`,
      documents: [
        {
          content: {
            dataSourceType: 'S3',
            s3: { s3Location: { uri } },
          },
          metadata: {
            type: 'S3_LOCATION',
            s3Location: { uri: `${uri}.metadata.json` },
          },
        },
      ],
    }),
  );
}
