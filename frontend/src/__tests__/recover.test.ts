// @vitest-environment jsdom
import type { MessageView } from '@kb/shared';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { recoverTurn } from '../api/recover';
import type { AppConfig } from '../config';

const mocks = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock('../api/http', () => ({ apiFetch: mocks.apiFetch }));

const config = { apiUrl: 'https://api.test' } as AppConfig;
const at = '2026-01-01T00:00:00.000Z';
const user = (content: string): MessageView => ({ role: 'user', content, citations: [], createdAt: at });
const answer = (content: string): MessageView => ({ role: 'assistant', content, citations: [], createdAt: at });
const reply = (messages: MessageView[]) => ({ json: async () => ({ messages }) });

afterEach(() => {
  mocks.apiFetch.mockReset();
  vi.restoreAllMocks();
});

function recover(overrides: Partial<Parameters<typeof recoverTurn>[0]> = {}) {
  return recoverTurn({
    config,
    sessionId: 'session/1',
    question: 'How much leave?',
    deadline: Date.now() + 60_000,
    signal: new AbortController().signal,
    onPending: vi.fn(),
    wait: vi.fn(async () => undefined),
    ...overrides,
  });
}

describe('recoverTurn', () => {
  it('polls while the answer is being written, then returns the saved conversation', async () => {
    const saved = [user('Earlier'), answer('Earlier answer'), user('How much leave?'), answer('25 days [ref:1]')];
    mocks.apiFetch
      .mockResolvedValueOnce(reply(saved.slice(0, 3)))
      .mockRejectedValueOnce(new TypeError('Load failed'))
      .mockResolvedValueOnce(reply(saved));
    const onPending = vi.fn();
    const result = await recover({ onPending });
    expect(result).toEqual({ state: 'answered', messages: saved });
    expect(onPending).toHaveBeenCalledTimes(1);
    expect(mocks.apiFetch).toHaveBeenCalledTimes(3);
    expect(mocks.apiFetch.mock.calls[0]?.[1]).toBe('/v1/sessions/session%2F1');
  });

  it('reports a question the server never saved after looking twice', async () => {
    const saved = [user('Earlier'), answer('Earlier answer')];
    mocks.apiFetch.mockResolvedValue(reply(saved));
    await expect(recover()).resolves.toEqual({ state: 'lost', messages: saved });
    expect(mocks.apiFetch).toHaveBeenCalledTimes(2);
  });

  it('gives up on an answer still missing at the deadline', async () => {
    mocks.apiFetch.mockResolvedValue(reply([user('How much leave?')]));
    await expect(recover({ deadline: Date.now() - 1 })).resolves.toEqual({ state: 'timeout' });
  });

  it('waits for a hidden app to return before polling', async () => {
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    mocks.apiFetch.mockResolvedValue(reply([user('How much leave?'), answer('Yes')]));
    const result = recover();
    await Promise.resolve();
    expect(mocks.apiFetch).not.toHaveBeenCalled();
    visibility.mockReturnValue('visible');
    document.dispatchEvent(new Event('visibilitychange'));
    await expect(result).resolves.toMatchObject({ state: 'answered' });
  });

  it('stops when the conversation is closed', async () => {
    const controller = new AbortController();
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    const result = recover({ signal: controller.signal });
    controller.abort(new DOMException('closed', 'AbortError'));
    await expect(result).rejects.toThrow('closed');
    expect(mocks.apiFetch).not.toHaveBeenCalled();
  });
});
