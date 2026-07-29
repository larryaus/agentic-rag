/**
 * Retrieve + ConverseStream preserves an extensible tool loop; RetrieveAndGenerate is a
 * closed box that Stage 2's external API tool and human-approval gate could not extend.
 */
import {
  ConverseStreamCommand,
  type ContentBlock,
  type Message,
  type ToolResultBlock,
} from '@aws-sdk/client-bedrock-runtime';
import type { Citation, SseEvent, Usage } from '@kb/shared';

import { bedrockRuntimeClient } from './clients';
import { resolveCitations } from './citations';
import { BedrockStreamError, errorMessage } from './errors';
import { SYSTEM_PROMPT } from './prompts';
import { retrieve, type RawChunk } from './retrieve';
import { SEARCH_TOOL } from './tools';

type AgentResult = {
  text: string;
  citations: Citation[];
  usage: Usage;
  stopReason: string;
};

type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };
type JsonObject = { [key: string]: JsonValue };

type TextBlockState = { kind: 'text'; text: string };
type ToolBlockState = {
  kind: 'tool';
  toolUseId: string;
  name: string;
  inputJson: string;
  input?: JsonObject;
};
type BlockState = TextBlockState | ToolBlockState;

const UUID_V4 =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function asObject(value: unknown): JsonObject {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error('Tool input must be a JSON object');
  }
  return value as JsonObject;
}

function streamExceptionName(event: object): string | undefined {
  if ('internalServerException' in event) return 'internalServerException';
  if ('modelStreamErrorException' in event) return 'modelStreamErrorException';
  if ('validationException' in event) return 'validationException';
  if ('throttlingException' in event) return 'throttlingException';
  if ('serviceUnavailableException' in event) {
    return 'serviceUnavailableException';
  }
  return undefined;
}

function buildAssistantContent(blocks: Map<number, BlockState>): ContentBlock[] {
  const content: ContentBlock[] = [];
  for (const [, block] of [...blocks.entries()].sort(
    ([left], [right]) => left - right,
  )) {
    if (block.kind === 'text') {
      const textBlock: ContentBlock = { text: block.text };
      content.push(textBlock);
    } else {
      const toolUseBlock: ContentBlock = {
        toolUse: {
          toolUseId: block.toolUseId,
          name: block.name,
          input: block.input ?? {},
        },
      };
      content.push(toolUseBlock);
    }
  }
  return content;
}

function requireBlockIndex(index: number | undefined): number {
  if (index === undefined) {
    throw new Error('Bedrock stream event omitted contentBlockIndex');
  }
  return index;
}

async function dispatchTool(opts: {
  block: ToolBlockState;
  topK: number;
  nextRef: () => number;
  citationIndex: Map<number, RawChunk>;
  emit: (event: SseEvent) => void;
}): Promise<ToolResultBlock> {
  const input = opts.block.input ?? {};
  opts.emit({ type: 'tool_use', name: opts.block.name, input });
  try {
    if (opts.block.name !== 'search_knowledge_base') {
      throw new Error(`Unknown tool: ${opts.block.name}`);
    }
    const query = input.query;
    if (typeof query !== 'string' || query.trim() === '') {
      throw new Error('search_knowledge_base requires a non-empty query');
    }
    const chunks = await retrieve({ query, topK: opts.topK });
    const results = chunks.map((chunk) => {
      const ref = opts.nextRef();
      if (UUID_V4.test(chunk.documentId)) {
        opts.citationIndex.set(ref, chunk);
        opts.emit({
          type: 'citation',
          ref,
          title: chunk.title,
          documentId: chunk.documentId,
          score: chunk.score,
          snippet: chunk.text.slice(0, 240),
        });
      }
      return {
        ref,
        title: chunk.title,
        documentId: chunk.documentId,
        score: chunk.score,
        text: chunk.text.slice(0, 1500),
      };
    });
    return {
      toolUseId: opts.block.toolUseId,
      status: 'success',
      content: [{ json: { results } }],
    };
  } catch (error) {
    return {
      toolUseId: opts.block.toolUseId,
      status: 'error',
      content: [{ text: errorMessage(error) }],
    };
  }
}

export async function runAgent(opts: {
  history: Message[];
  userMessage: string;
  emit: (event: SseEvent) => void;
  topK: number;
  maxIterations: number;
  modelId: string;
}): Promise<AgentResult> {
  const messages: Message[] = [
    ...opts.history,
    { role: 'user', content: [{ text: opts.userMessage }] },
  ];
  const citationIndex = new Map<number, RawChunk>();
  let refCounter = 0;
  let text = '';
  let stopReason = 'end_turn';
  const usage: Usage = { inputTokens: 0, outputTokens: 0 };

  for (let iteration = 0; iteration < opts.maxIterations; iteration += 1) {
    const response = await bedrockRuntimeClient.send(
      new ConverseStreamCommand({
        modelId: opts.modelId,
        messages,
        system: [{ text: SYSTEM_PROMPT }],
        toolConfig: { tools: [SEARCH_TOOL] },
        inferenceConfig: { maxTokens: 2048 },
      }),
    );
    if (response.stream === undefined) {
      throw new Error('Bedrock returned no response stream');
    }

    const blocks = new Map<number, BlockState>();
    stopReason = 'end_turn';
    for await (const event of response.stream) {
      const exceptionName = streamExceptionName(event);
      if (exceptionName !== undefined) {
        throw new BedrockStreamError(exceptionName, {
          text,
          citations: resolveCitations(text, citationIndex),
          usage: { ...usage },
          stopReason: 'error',
        });
      }

      const start = event.contentBlockStart;
      if (start?.start?.toolUse !== undefined) {
        const index = requireBlockIndex(start.contentBlockIndex);
        blocks.set(index, {
          kind: 'tool',
          toolUseId: start.start.toolUse.toolUseId ?? '',
          name: start.start.toolUse.name ?? '',
          inputJson: '',
        });
      }

      const delta = event.contentBlockDelta;
      if (delta !== undefined) {
        const index = requireBlockIndex(delta.contentBlockIndex);
        if (delta.delta?.text !== undefined) {
          const existing = blocks.get(index);
          if (existing?.kind === 'text') {
            existing.text += delta.delta.text;
          } else {
            blocks.set(index, { kind: 'text', text: delta.delta.text });
          }
          text += delta.delta.text;
          opts.emit({ type: 'text', delta: delta.delta.text });
        }
        if (delta.delta?.toolUse?.input !== undefined) {
          const block = blocks.get(index);
          if (block?.kind !== 'tool') {
            throw new Error('Received tool input before tool start');
          }
          block.inputJson += delta.delta.toolUse.input;
        }
      }

      const stopped = event.contentBlockStop;
      if (stopped !== undefined) {
        const index = requireBlockIndex(stopped.contentBlockIndex);
        const block = blocks.get(index);
        if (block?.kind === 'tool') {
          block.input = asObject(JSON.parse(block.inputJson) as unknown);
        }
      }

      if (event.messageStop?.stopReason !== undefined) {
        stopReason = event.messageStop.stopReason;
      }
      if (event.metadata?.usage !== undefined) {
        usage.inputTokens += event.metadata.usage.inputTokens ?? 0;
        usage.outputTokens += event.metadata.usage.outputTokens ?? 0;
      }
    }

    const assistantContent = buildAssistantContent(blocks);
    if (assistantContent.length > 0) {
      messages.push({ role: 'assistant', content: assistantContent });
    }
    if (stopReason !== 'tool_use') {
      break;
    }
    if (iteration === opts.maxIterations - 1) {
      stopReason = 'max_iterations';
      break;
    }

    const toolBlocks = [...blocks.values()].filter(
      (block): block is ToolBlockState => block.kind === 'tool',
    );
    const results: ToolResultBlock[] = [];
    for (const block of toolBlocks) {
      results.push(
        await dispatchTool({
          block,
          topK: opts.topK,
          nextRef: () => {
            refCounter += 1;
            return refCounter;
          },
          citationIndex,
          emit: opts.emit,
        }),
      );
    }
    messages.push({
      role: 'user',
      content: results.map((toolResult) => ({ toolResult })),
    });
  }

  if (stopReason === 'max_iterations' && text === '') {
    text =
      'I could not complete this question within the allowed number of search steps. Please try narrowing it.';
  }
  return {
    text,
    citations: resolveCitations(text, citationIndex),
    usage,
    stopReason,
  };
}
