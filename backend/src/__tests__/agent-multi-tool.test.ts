import {
  BedrockRuntimeClient,
  ConverseStreamCommand,
  type ConverseStreamOutput,
} from '@aws-sdk/client-bedrock-runtime';
import { mockClient } from 'aws-sdk-client-mock';
import { beforeEach, expect, it, vi } from 'vitest';

import { runAgent } from '../lib/agent';
import { retrieve } from '../lib/retrieve';
import { chunk, scripted, textTurn } from './agent-fixtures';

vi.mock('../lib/retrieve', () => ({ retrieve: vi.fn() }));

const bedrock = mockClient(BedrockRuntimeClient);

beforeEach(() => {
  bedrock.reset();
  vi.mocked(retrieve).mockReset();
});

it('runs every tool block and returns all results in one user message', async () => {
  const toolEvents: ConverseStreamOutput[] = [
    {
      contentBlockStart: {
        contentBlockIndex: 0,
        start: {
          toolUse: {
            toolUseId: 'first',
            name: 'search_knowledge_base',
          },
        },
      },
    },
    {
      contentBlockDelta: {
        contentBlockIndex: 0,
        delta: { toolUse: { input: '{"query":"alpha"}' } },
      },
    },
    { contentBlockStop: { contentBlockIndex: 0 } },
    {
      contentBlockStart: {
        contentBlockIndex: 1,
        start: {
          toolUse: {
            toolUseId: 'second',
            name: 'search_knowledge_base',
          },
        },
      },
    },
    {
      contentBlockDelta: {
        contentBlockIndex: 1,
        delta: { toolUse: { input: '{"query":"beta"}' } },
      },
    },
    { contentBlockStop: { contentBlockIndex: 1 } },
    { messageStop: { stopReason: 'tool_use' } },
  ];
  bedrock
    .on(ConverseStreamCommand)
    .resolvesOnce({ stream: scripted(toolEvents) })
    .resolvesOnce({ stream: textTurn('Answer [ref:1] [ref:2]') });
  vi.mocked(retrieve)
    .mockResolvedValueOnce([chunk(0)])
    .mockResolvedValueOnce([chunk(1)]);

  await runAgent({
    history: [],
    userMessage: 'both',
    emit: () => undefined,
    topK: 8,
    maxIterations: 6,
    modelId: 'test-model',
  });

  expect(retrieve).toHaveBeenCalledTimes(2);
  const messages =
    bedrock.commandCalls(ConverseStreamCommand)[1]?.args[0].input.messages;
  const resultMessage = messages?.[2];
  expect(resultMessage?.role).toBe('user');
  const resultContent = resultMessage?.content;
  expect(resultContent).toHaveLength(2);
  if (resultContent === undefined) {
    throw new Error('Expected the tool-result message to contain content');
  }
  expect(resultContent.map((block) => block.toolResult?.toolUseId)).toEqual([
    'first',
    'second',
  ]);
});
