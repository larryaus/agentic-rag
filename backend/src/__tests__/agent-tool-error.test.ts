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

it('returns malformed tool input to the model instead of failing the turn', async () => {
  bedrock
    .on(ConverseStreamCommand)
    .resolvesOnce({
      stream: toolTurn({ toolUseId: 'malformed', fragments: ['{"query":'] }),
    })
    .resolvesOnce({ stream: textTurn('Let me answer without searching.') });

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
  expect(messages?.[1]?.content?.[0]?.toolUse).toEqual(
    expect.objectContaining({ toolUseId: 'malformed', input: {} }),
  );
  const toolResult = messages?.[2]?.content?.[0]?.toolResult;
  expect(toolResult).toEqual(
    expect.objectContaining({ toolUseId: 'malformed', status: 'error' }),
  );
  expect(toolResult?.content?.[0]?.text).toMatch(
    /^Error: Tool input was not a valid JSON object: SyntaxError/,
  );
  expect(retrieve).not.toHaveBeenCalled();
  expect(result).toEqual(
    expect.objectContaining({
      text: 'Let me answer without searching.',
      stopReason: 'end_turn',
    }),
  );
});

it('treats a tool call with no streamed input as an empty object', async () => {
  bedrock
    .on(ConverseStreamCommand)
    .resolvesOnce({
      stream: toolTurn({ toolUseId: 'empty', fragments: [] }),
    })
    .resolvesOnce({ stream: textTurn('I need a more specific question.') });

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
  expect(messages?.[2]?.content?.[0]?.toolResult).toEqual(
    expect.objectContaining({
      toolUseId: 'empty',
      status: 'error',
      content: [
        { text: 'Error: search_knowledge_base requires a non-empty query' },
      ],
    }),
  );
  expect(result.stopReason).toBe('end_turn');
});
