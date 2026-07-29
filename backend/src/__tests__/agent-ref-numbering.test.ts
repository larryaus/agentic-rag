import {
  BedrockRuntimeClient,
  ConverseStreamCommand,
} from '@aws-sdk/client-bedrock-runtime';
import { mockClient } from 'aws-sdk-client-mock';
import { beforeEach, expect, it, vi } from 'vitest';

import { runAgent } from '../lib/agent';
import { retrieve } from '../lib/retrieve';
import { chunk, textTurn, toolTurn } from './agent-fixtures';

vi.mock('../lib/retrieve', () => ({ retrieve: vi.fn() }));

const bedrock = mockClient(BedrockRuntimeClient);

beforeEach(() => {
  bedrock.reset();
  vi.mocked(retrieve).mockReset();
});

it('keeps refs monotonic across retrieval calls and resolves later refs correctly', async () => {
  bedrock
    .on(ConverseStreamCommand)
    .resolvesOnce({
      stream: toolTurn({
        toolUseId: 'one',
        fragments: ['{"query":"one"}'],
      }),
    })
    .resolvesOnce({
      stream: toolTurn({
        toolUseId: 'two',
        fragments: ['{"query":"two"}'],
      }),
    })
    .resolvesOnce({ stream: textTurn('The later source says this [ref:3].') });
  vi.mocked(retrieve)
    .mockResolvedValueOnce([chunk(0), chunk(1)])
    .mockResolvedValueOnce([chunk(2), chunk(3)]);
  const emittedRefs: number[] = [];

  const result = await runAgent({
    history: [],
    userMessage: 'question',
    emit: (event) => {
      if (event.type === 'citation') emittedRefs.push(event.ref);
    },
    topK: 8,
    maxIterations: 6,
    modelId: 'test-model',
  });

  expect(emittedRefs).toEqual([1, 2, 3, 4]);
  expect(result.citations).toEqual([
    expect.objectContaining({
      ref: 3,
      documentId: chunk(2).documentId,
      title: chunk(2).title,
    }),
  ]);
});
