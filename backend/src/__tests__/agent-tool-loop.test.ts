import {
  BedrockRuntimeClient,
  ConverseStreamCommand,
} from '@aws-sdk/client-bedrock-runtime';
import { mockClient } from 'aws-sdk-client-mock';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { runAgent } from '../lib/agent';
import { retrieve } from '../lib/retrieve';
import { chunk, textTurn, toolTurn } from './agent-fixtures';

vi.mock('../lib/retrieve', () => ({ retrieve: vi.fn() }));

const bedrock = mockClient(BedrockRuntimeClient);

describe('agent tool loop', () => {
  beforeEach(() => {
    bedrock.reset();
    vi.mocked(retrieve).mockReset();
  });

  it('buffers fragmented input and reconstructs Bedrock message order', async () => {
    bedrock
      .on(ConverseStreamCommand)
      .resolvesOnce({
        stream: toolTurn({
          toolUseId: 'tool-123',
          fragments: ['{"que', 'ry":"leave', ' policy"}'],
        }),
      })
      .resolvesOnce({ stream: textTurn('Annual leave is documented [ref:1].') });
    vi.mocked(retrieve).mockResolvedValue([chunk(0, 'leave policy')]);
    const eventTypes: string[] = [];

    const result = await runAgent({
      history: [],
      userMessage: 'What is the leave policy?',
      emit: (event) => eventTypes.push(event.type),
      topK: 8,
      maxIterations: 6,
      modelId: 'test-model',
    });

    expect(retrieve).toHaveBeenCalledWith({
      query: 'leave policy',
      topK: 8,
    });
    const secondCall = bedrock.commandCalls(ConverseStreamCommand)[1];
    const messages = secondCall?.args[0].input.messages;
    expect(messages?.[1]).toEqual({
      role: 'assistant',
      content: [
        {
          toolUse: {
            toolUseId: 'tool-123',
            name: 'search_knowledge_base',
            input: { query: 'leave policy' },
          },
        },
      ],
    });
    expect(messages?.[2]).toEqual({
      role: 'user',
      content: [
        {
          toolResult: expect.objectContaining({
            toolUseId: 'tool-123',
            status: 'success',
          }),
        },
      ],
    });
    expect(eventTypes).toEqual(['tool_use', 'citation', 'text']);
    expect(result.citations).toHaveLength(1);
  });
});
