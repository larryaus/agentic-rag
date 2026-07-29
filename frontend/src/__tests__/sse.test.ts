import type { SseEvent } from '@kb/shared';
import { describe, expect, it } from 'vitest';

import { consumeSse } from '../api/sse';

const encoder = new TextEncoder();

function responseFrom(chunks: Uint8Array[]): Response {
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        chunks.forEach((chunk) => controller.enqueue(chunk));
        controller.close();
      },
    }),
  );
}

async function eventsFrom(chunks: Uint8Array[]): Promise<SseEvent[]> {
  const events: SseEvent[] = [];
  await consumeSse(responseFrom(chunks), (event) => events.push(event));
  return events;
}

describe('SSE parser', () => {
  it('parses multiple frames from one network chunk', async () => {
    const bytes = encoder.encode(
      'event: session\ndata: {"sessionId":"s"}\n\n' +
        'event: text\ndata: {"delta":"hello"}\n\n',
    );
    await expect(eventsFrom([bytes])).resolves.toEqual([
      { type: 'session', sessionId: 's' },
      { type: 'text', delta: 'hello' },
    ]);
  });

  it('buffers a frame split across chunks', async () => {
    const first = encoder.encode('event: text\ndata: {"delta":"hel');
    const second = encoder.encode('lo"}\n\n');
    await expect(eventsFrom([first, second])).resolves.toEqual([
      { type: 'text', delta: 'hello' },
    ]);
  });

  it('buffers JSON split in the middle of an object', async () => {
    const frame =
      'event: citation\ndata: {"ref":1,"title":"doc","documentId":"id","score":0.9,"snippet":"text"}\n\n';
    const boundary = frame.indexOf('"documentId"');
    await expect(
      eventsFrom([
        encoder.encode(frame.slice(0, boundary)),
        encoder.encode(frame.slice(boundary)),
      ]),
    ).resolves.toEqual([
      {
        type: 'citation',
        ref: 1,
        title: 'doc',
        documentId: 'id',
        score: 0.9,
        snippet: 'text',
      },
    ]);
  });

  it('does not emit a trailing frame without a blank-line terminator', async () => {
    await expect(
      eventsFrom([encoder.encode('event: text\ndata: {"delta":"partial"}')]),
    ).resolves.toEqual([]);
  });

  it('preserves a Chinese character split across UTF-8 chunks', async () => {
    const frame = 'event: text\ndata: {"delta":"知识"}\n\n';
    const bytes = encoder.encode(frame);
    const character = encoder.encode('知');
    const start = bytes.findIndex(
      (byte, index) =>
        byte === character[0] &&
        bytes[index + 1] === character[1] &&
        bytes[index + 2] === character[2],
    );
    const split = start + 1;
    await expect(
      eventsFrom([bytes.slice(0, split), bytes.slice(split)]),
    ).resolves.toEqual([{ type: 'text', delta: '知识' }]);
  });

  it('ignores unknown event names', async () => {
    await expect(
      eventsFrom([
        encoder.encode('event: future\ndata: {"value":1}\n\n'),
      ]),
    ).resolves.toEqual([]);
  });
});
