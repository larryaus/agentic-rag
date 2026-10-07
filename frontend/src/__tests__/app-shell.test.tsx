// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import type { SseEvent } from '@kb/shared';
import { afterEach, expect, it, vi } from 'vitest';

import type { AppConfig } from '../config';
import { AppShell } from '../components/AppShell';

const mocks = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  getAccessToken: vi.fn(),
  streamChat: vi.fn(),
}));
vi.mock('../api/http', () => ({ apiFetch: mocks.apiFetch }));
vi.mock('../auth/auth', () => ({
  getAccessToken: mocks.getAccessToken,
  logout: vi.fn(),
}));
vi.mock('../api/sse', () => ({ streamChat: mocks.streamChat }));
vi.mock('../components/DocumentPanel', () => ({ DocumentPanel: () => null }));
vi.mock('../components/SessionList', () => ({
  SessionList: (props: {
    onSelect: (id: string) => void;
    onNew: () => void;
    activeSessionId?: string;
    refreshVersion: number;
  }) => (
    <nav
      data-active-session={props.activeSessionId ?? ''}
      data-refresh-version={props.refreshVersion}
    >
      <button onClick={() => props.onSelect('a')}>Conversation A</button>
      <button onClick={() => props.onSelect('b')}>Conversation B</button>
      <button onClick={props.onNew}>New conversation</button>
    </nav>
  ),
}));

const config: AppConfig = {
  userPoolId: 'pool',
  userPoolClientId: 'client',
  cognitoDomain: 'https://auth.test',
  apiUrl: 'https://api.test',
  chatUrl: 'https://chat.test',
  awsRegion: 'us-east-1',
};

function response(text: string): Response {
  return new Response(
    JSON.stringify({
      messages: [
        {
          role: 'assistant',
          content: text,
          citations: [],
          createdAt: '2026-10-05T00:00:00Z',
        },
      ],
    }),
  );
}

afterEach(cleanup);

it('keeps the latest selection when conversation requests finish out of order', async () => {
  let finishA: ((value: Response) => void) | undefined;
  mocks.apiFetch.mockImplementation((_config: AppConfig, path: string) =>
    path.endsWith('/a')
      ? new Promise<Response>((resolve) => {
          finishA = resolve;
        })
      : Promise.resolve(response('Answer B')),
  );
  render(<AppShell config={config} />);
  fireEvent.click(screen.getByRole('button', { name: 'Conversation A' }));
  fireEvent.click(screen.getByRole('button', { name: 'Conversation B' }));
  expect(await screen.findByText('Answer B')).toBeInTheDocument();

  await act(async () => {
    finishA?.(response('Answer A'));
  });

  expect(screen.getByText('Answer B')).toBeInTheDocument();
  expect(screen.queryByText('Answer A')).not.toBeInTheDocument();
});

it('does not reopen an old conversation after starting a new one during loading', async () => {
  let finishA: ((value: Response) => void) | undefined;
  mocks.apiFetch.mockImplementation(
    () =>
      new Promise<Response>((resolve) => {
        finishA = resolve;
      }),
  );
  render(<AppShell config={config} />);
  fireEvent.click(screen.getByRole('button', { name: 'Conversation A' }));
  fireEvent.click(screen.getByRole('button', { name: 'New conversation' }));

  await act(async () => {
    finishA?.(response('Answer A'));
  });

  expect(
    screen.getByText('Ask your company knowledge base'),
  ).toBeInTheDocument();
  expect(screen.queryByText('Answer A')).not.toBeInTheDocument();
});

it('cancels the active response immediately when navigation begins', async () => {
  let signal: AbortSignal | undefined;
  let emit: ((event: SseEvent) => void) | undefined;
  let finish: (() => void) | undefined;
  mocks.getAccessToken.mockResolvedValue('token');
  mocks.streamChat.mockImplementation(
    (options: { signal: AbortSignal; onEvent: (event: SseEvent) => void }) => {
      signal = options.signal;
      emit = options.onEvent;
      return new Promise<void>((resolve) => {
        finish = resolve;
      });
    },
  );
  mocks.apiFetch.mockResolvedValue(response('Answer B'));
  render(<AppShell config={config} />);
  fireEvent.change(screen.getByRole('textbox'), {
    target: { value: 'Old question' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Send' }));
  await waitFor(() => expect(signal).toBeDefined());
  fireEvent.click(screen.getByRole('button', { name: 'Conversation B' }));
  expect(signal?.aborted).toBe(true);
  expect(await screen.findByText('Answer B')).toBeInTheDocument();

  await act(async () => {
    emit?.({ type: 'text', delta: ' OLD RESPONSE' });
    finish?.();
  });

  expect(screen.getByText('Answer B')).toBeInTheDocument();
  expect(screen.queryByText(/OLD RESPONSE/)).not.toBeInTheDocument();
});

it('shows a load error and restores an available composer', async () => {
  mocks.apiFetch.mockRejectedValue(new Error('Conversation unavailable'));
  render(<AppShell config={config} />);
  fireEvent.click(screen.getByRole('button', { name: 'Conversation A' }));

  expect(await screen.findByRole('alert')).toHaveTextContent(
    'Conversation unavailable',
  );
  expect(screen.getByRole('textbox')).toBeInTheDocument();
});

it('refreshes the sidebar when navigation aborts a stream that created a session', async () => {
  let emit: ((event: SseEvent) => void) | undefined;
  let finish: (() => void) | undefined;
  mocks.getAccessToken.mockResolvedValue('token');
  mocks.streamChat.mockImplementation(
    (options: { onEvent: (event: SseEvent) => void }) => {
      emit = options.onEvent;
      return new Promise<void>((resolve) => { finish = resolve; });
    },
  );
  mocks.apiFetch.mockResolvedValue(response('Answer B'));
  render(<AppShell config={config} />);
  fireEvent.change(screen.getByRole('textbox'), {
    target: { value: 'New question' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Send' }));
  await waitFor(() => expect(emit).toBeDefined());
  act(() => { emit?.({ type: 'session', sessionId: 'new-session' }); });
  fireEvent.click(screen.getByRole('button', { name: 'Conversation B' }));

  expect(await screen.findByText('Answer B')).toBeInTheDocument();
  expect(screen.getByRole('navigation')).toHaveAttribute(
    'data-refresh-version', '1',
  );
  expect(screen.getByRole('navigation')).toHaveAttribute('data-active-session', 'b');
  await act(async () => {
    emit?.({
      type: 'done', sessionId: 'new-session', stopReason: 'end_turn',
      usage: { inputTokens: 1, outputTokens: 1 },
    });
    finish?.();
  });
  expect(screen.getByRole('navigation')).toHaveAttribute(
    'data-refresh-version', '1',
  );
});

it('opens a new conversation after a failed selection instead of reusing stale messages and session', async () => {
  mocks.apiFetch.mockImplementation((_config: AppConfig, path: string) =>
    path.endsWith('/a')
      ? Promise.resolve(response('Old stored answer'))
      : Promise.reject(new Error('Conversation unavailable')),
  );
  mocks.getAccessToken.mockResolvedValue('token');
  mocks.streamChat.mockImplementation(
    (options: { onEvent: (event: SseEvent) => void }) => {
      options.onEvent({ type: 'text', delta: 'Newly streamed answer' });
      return Promise.resolve();
    },
  );
  render(<AppShell config={config} />);
  fireEvent.click(screen.getByRole('button', { name: 'Conversation A' }));
  expect(await screen.findByText('Old stored answer')).toBeInTheDocument();
  fireEvent.change(screen.getByRole('textbox'), {
    target: { value: 'Old question' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Send' }));
  expect(await screen.findByText('Newly streamed answer')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Conversation B' }));

  expect(await screen.findByRole('alert')).toHaveTextContent(
    'Conversation unavailable',
  );
  expect(screen.getByText('Ask your company knowledge base')).toBeInTheDocument();
  expect(screen.queryByText('Old stored answer')).not.toBeInTheDocument();
  expect(screen.getByRole('navigation')).toHaveAttribute('data-active-session', '');
  fireEvent.change(screen.getByRole('textbox'), {
    target: { value: 'New question' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Send' }));
  await waitFor(() => expect(mocks.streamChat).toHaveBeenCalledTimes(2));
  expect(mocks.streamChat.mock.calls[1]?.[0]).not.toHaveProperty('sessionId');
});
