import type { ConverseStreamOutput } from '@aws-sdk/client-bedrock-runtime';

export const DOC_IDS = [
  '11111111-1111-4111-8111-111111111111',
  '22222222-2222-4222-8222-222222222222',
  '33333333-3333-4333-8333-333333333333',
  '44444444-4444-4444-8444-444444444444',
] as const;

export function scripted(
  events: ConverseStreamOutput[],
): AsyncIterable<ConverseStreamOutput> {
  return {
    async *[Symbol.asyncIterator]() {
      yield* events;
    },
  };
}

export function toolTurn(opts: {
  toolUseId: string;
  fragments: string[];
  index?: number;
  name?: string;
}): AsyncIterable<ConverseStreamOutput> {
  const index = opts.index ?? 0;
  return scripted([
    { messageStart: { role: 'assistant' } },
    {
      contentBlockStart: {
        contentBlockIndex: index,
        start: {
          toolUse: {
            toolUseId: opts.toolUseId,
            name: opts.name ?? 'search_knowledge_base',
          },
        },
      },
    },
    ...opts.fragments.map(
      (input): ConverseStreamOutput => ({
        contentBlockDelta: {
          contentBlockIndex: index,
          delta: { toolUse: { input } },
        },
      }),
    ),
    { contentBlockStop: { contentBlockIndex: index } },
    { messageStop: { stopReason: 'tool_use' } },
    {
      metadata: {
        usage: { inputTokens: 10, outputTokens: 2, totalTokens: 12 },
        metrics: { latencyMs: 1 },
      },
    },
  ]);
}

export function textTurn(text: string): AsyncIterable<ConverseStreamOutput> {
  return scripted([
    { messageStart: { role: 'assistant' } },
    {
      contentBlockDelta: {
        contentBlockIndex: 0,
        delta: { text },
      },
    },
    { contentBlockStop: { contentBlockIndex: 0 } },
    { messageStop: { stopReason: 'end_turn' } },
    {
      metadata: {
        usage: { inputTokens: 5, outputTokens: 4, totalTokens: 9 },
        metrics: { latencyMs: 1 },
      },
    },
  ]);
}

export function chunk(
  index: number,
  text = `chunk-${index}`,
): {
  text: string;
  sourceUri: string;
  documentId: string;
  title: string;
  score: number;
} {
  const documentId = DOC_IDS[index % DOC_IDS.length] ?? DOC_IDS[0];
  return {
    text,
    sourceUri: `s3://bucket/${documentId}/doc-${index}.md`,
    documentId,
    title: `doc-${index}.md`,
    score: 0.9 - index / 100,
  };
}
