import {
  BedrockRuntimeClient,
  ConverseStreamCommand,
} from '@aws-sdk/client-bedrock-runtime';
import { mockClient } from 'aws-sdk-client-mock';
import { beforeEach, expect, it, vi } from 'vitest';

import { runAgent } from '../lib/agent';
import { retrieve } from '../lib/retrieve';
import { textTurn, toolTurn } from './agent-fixtures';

vi.mock('../lib/retrieve', () => ({ retrieve: vi.fn() }));

const bedrock = mockClient(BedrockRuntimeClient);

beforeEach(() => {
  bedrock.reset();
  vi.mocked(retrieve).mockReset();
});

it('returns tool errors to the model and lets the loop finish', async () => {
  bedrock
    .on(ConverseStreamCommand)
    .resolvesOnce({
      stream: toolTurn({
        toolUseId: 'broken',
        fragments: ['{"query":"failure"}'],
      }),
    })
    .resolvesOnce({ stream: textTurn('The search was unavailable.') });
  vi.mocked(retrieve).mockRejectedValue(new Error('temporary outage'));

  const result = await runAgent({
    history: [],
    userMessage: 'question',
    emit: () => undefined,
    topK: 8,
    maxIterations: 6,
    modelId: 'test-model',
  });

  const messages =
    bedrock.commandCalls(ConverseStreamCommand)[1]?.args[0].input.messages;
  const resultContent = messages?.[2]?.content;
  if (resultContent === undefined) {
    throw new Error('Expected the tool-result message to contain content');
  }
  expect(resultContent[0]?.toolResult).toEqual(
    expect.objectContaining({
      toolUseId: 'broken',
      status: 'error',
      content: [{ text: 'Error: temporary outage' }],
    }),
  );
  expect(result.stopReason).toBe('end_turn');
});
