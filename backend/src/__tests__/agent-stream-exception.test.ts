import {
  BedrockRuntimeClient,
  ConverseStreamCommand,
} from '@aws-sdk/client-bedrock-runtime';
import { mockClient } from 'aws-sdk-client-mock';
import { expect, it } from 'vitest';

import { runAgent } from '../lib/agent';
import { BedrockStreamError } from '../lib/errors';
import { scripted } from './agent-fixtures';

const bedrock = mockClient(BedrockRuntimeClient);

it('surfaces a typed stream error carrying accumulated output', async () => {
  bedrock.reset();
  bedrock.on(ConverseStreamCommand).resolves({
    stream: scripted([
      {
        contentBlockDelta: {
          contentBlockIndex: 0,
          delta: { text: 'Partial answer' },
        },
      },
      {
        modelStreamErrorException: {
          name: 'ModelStreamErrorException',
          $fault: 'client',
          $metadata: {},
          message: 'stream interrupted',
          originalMessage: 'upstream',
          originalStatusCode: 500,
        },
      },
    ]),
  });

  const error: unknown = await runAgent({
    history: [],
    userMessage: 'question',
    emit: () => undefined,
    topK: 8,
    maxIterations: 6,
    modelId: 'test-model',
  }).catch((caught: unknown) => caught);

  expect(error).toBeInstanceOf(BedrockStreamError);
  expect(error).toMatchObject({
    name: 'BedrockStreamError',
    partialResult: {
      text: 'Partial answer',
      citations: [],
      usage: { inputTokens: 0, outputTokens: 0 },
      stopReason: 'error',
    },
  });
});
