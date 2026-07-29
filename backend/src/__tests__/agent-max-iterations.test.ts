import {
  BedrockRuntimeClient,
  ConverseStreamCommand,
} from '@aws-sdk/client-bedrock-runtime';
import { mockClient } from 'aws-sdk-client-mock';
import { beforeEach, expect, it, vi } from 'vitest';

import { runAgent } from '../lib/agent';
import { retrieve } from '../lib/retrieve';
import { chunk, toolTurn } from './agent-fixtures';

vi.mock('../lib/retrieve', () => ({ retrieve: vi.fn() }));

const bedrock = mockClient(BedrockRuntimeClient);

beforeEach(() => {
  bedrock.reset();
  vi.mocked(retrieve).mockReset();
});

it('counts model iterations and does not dispatch an unusable final tool call', async () => {
  bedrock.on(ConverseStreamCommand).callsFake(() => ({
    stream: toolTurn({
      toolUseId: 'again',
      fragments: ['{"query":"again"}'],
    }),
  }));
  vi.mocked(retrieve).mockResolvedValue([chunk(0)]);
  const maxIterations = 3;

  const result = await runAgent({
    history: [],
    userMessage: 'question',
    emit: () => undefined,
    topK: 8,
    maxIterations,
    modelId: 'test-model',
  });

  expect(bedrock.commandCalls(ConverseStreamCommand)).toHaveLength(maxIterations);
  expect(retrieve).toHaveBeenCalledTimes(maxIterations - 1);
  expect(result.stopReason).toBe('max_iterations');
  expect(result.text).toBe(
    'I could not complete this question within the allowed number of search steps. Please try narrowing it.',
  );
});
