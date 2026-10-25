import {
  BedrockRuntimeClient,
  ConverseStreamCommand,
} from '@aws-sdk/client-bedrock-runtime';
import { mockClient } from 'aws-sdk-client-mock';
import { beforeEach, expect, it, vi } from 'vitest';

import { runAgent } from '../lib/agent';
import { retrieve } from '../lib/retrieve';
import { GUARDRAIL, chunk, textTurn, toolTurn } from './agent-fixtures';

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
    guardrail: GUARDRAIL,
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

it('lets the model correct malformed tool input and answer with a citation', async () => {
  bedrock
    .on(ConverseStreamCommand)
    .resolvesOnce({
      stream: toolTurn({ toolUseId: 'malformed', fragments: ['{"query":'] }),
    })
    .resolvesOnce({
      stream: toolTurn({
        toolUseId: 'corrected',
        fragments: ['{"query":"leave policy"}'],
      }),
    })
    .resolvesOnce({ stream: textTurn('Annual leave is 20 days [ref:1].') });
  vi.mocked(retrieve).mockResolvedValue([chunk(0, 'leave policy')]);

  const result = await runAgent({
    history: [],
    userMessage: 'question',
    emit: () => undefined,
    topK: 8,
    maxIterations: 6,
    modelId: 'test-model',
    guardrail: GUARDRAIL,
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
  expect(retrieve).toHaveBeenCalledOnce();
  expect(retrieve).toHaveBeenCalledWith({ query: 'leave policy', topK: 8 });
  expect(result).toEqual(
    expect.objectContaining({
      text: 'Annual leave is 20 days [ref:1].',
      citations: [expect.objectContaining({ ref: 1, title: 'doc-0.md' })],
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
    guardrail: GUARDRAIL,
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
