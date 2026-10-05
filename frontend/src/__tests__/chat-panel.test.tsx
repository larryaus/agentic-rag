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
import type { MessageView, SseEvent } from '@kb/shared';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AppConfig } from '../config';
import { ChatPanel } from '../components/ChatPanel';

const mocks = vi.hoisted(() => ({
  getAccessToken: vi.fn(),
  streamChat: vi.fn(),
}));

vi.mock('../auth/auth', () => ({ getAccessToken: mocks.getAccessToken }));
vi.mock('../api/sse', () => ({ streamChat: mocks.streamChat }));

const config: AppConfig = {
  userPoolId: 'pool',
  userPoolClientId: 'client',
  cognitoDomain: 'https://auth.example.test',
  apiUrl: 'https://api.example.test',
  chatUrl: 'https://chat.example.test',
  awsRegion: 'us-east-1',
};
const initialMessages: MessageView[] = [];

afterEach(cleanup);

describe('ChatPanel session assignment', () => {
  it('preserves optimistic messages when a new stream assigns its session ID', async () => {
    let finishStream: (() => void) | undefined;
    let signal: AbortSignal | undefined;
    mocks.getAccessToken.mockResolvedValue('token');
    mocks.streamChat.mockImplementation(
      (options: { signal: AbortSignal }) =>
        new Promise<void>((resolve) => {
          finishStream = resolve;
          signal = options.signal;
        }),
    );
    const baseProps = {
      config,
      initialMessages,
      onSession: vi.fn(),
      onCompleted: vi.fn(),
    };
    const view = render(<ChatPanel {...baseProps} />);

    fireEvent.change(screen.getByRole('textbox'), {
      target: { value: 'First question' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText('First question')).toBeInTheDocument();
    await waitFor(() => expect(signal).toBeDefined());

    view.rerender(
      <ChatPanel
        {...baseProps}
        sessionId="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
      />,
    );

    expect(screen.getByText('First question')).toBeInTheDocument();
    expect(signal?.aborted).toBe(false);
    finishStream?.();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument(),
    );
  });
});

describe('ChatPanel request isolation', () => {
  it('refreshes a completed session only once when navigation aborts before the stream closes', async () => {
    let emit: ((event: SseEvent) => void) | undefined;
    let finish: (() => void) | undefined;
    mocks.getAccessToken.mockResolvedValue('token');
    mocks.streamChat.mockImplementation(
      (options: { onEvent: (event: SseEvent) => void }) => {
        emit = options.onEvent;
        return new Promise<void>((resolve) => { finish = resolve; });
      },
    );
    const onCompleted = vi.fn();
    const view = render(
      <ChatPanel
        config={config}
        initialMessages={initialMessages}
        onSession={vi.fn()}
        onCompleted={onCompleted}
      />,
    );
    fireEvent.change(screen.getByRole('textbox'), {
      target: { value: 'Question' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() => expect(emit).toBeDefined());
    act(() => {
      emit?.({ type: 'session', sessionId: 'new-session' });
      emit?.({
        type: 'done', sessionId: 'new-session', stopReason: 'end_turn',
        usage: { inputTokens: 1, outputTokens: 1 },
      });
    });
    view.unmount();
    await act(async () => { finish?.(); });

    expect(onCompleted).toHaveBeenCalledTimes(1);
  });

  it('aborts a replaced conversation and ignores its late text, session, and completion events', async () => {
    let emit: ((event: SseEvent) => void) | undefined;
    let finish: (() => void) | undefined;
    let signal: AbortSignal | undefined;
    mocks.getAccessToken.mockResolvedValue('token');
    mocks.streamChat.mockImplementation(
      (options: {
        onEvent: (event: SseEvent) => void;
        signal: AbortSignal;
      }) => {
        emit = options.onEvent;
        signal = options.signal;
        return new Promise<void>((resolve) => {
          finish = resolve;
        });
      },
    );
    const callbacks = { onSession: vi.fn(), onCompleted: vi.fn() };
    const view = render(
      <ChatPanel
        config={config}
        sessionId="old"
        initialMessages={initialMessages}
        {...callbacks}
      />,
    );
    fireEvent.change(screen.getByRole('textbox'), {
      target: { value: 'Old question' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() => expect(emit).toBeDefined());
    view.rerender(
      <ChatPanel
        config={config}
        sessionId="new"
        initialMessages={[
          {
            role: 'assistant',
            content: 'New answer',
            citations: [],
            createdAt: '2026-10-05T00:00:00Z',
          },
        ]}
        {...callbacks}
      />,
    );

    await act(async () => {
      emit?.({ type: 'text', delta: ' OLD RESPONSE' });
      emit?.({ type: 'session', sessionId: 'old' });
      emit?.({
        type: 'done',
        sessionId: 'old',
        stopReason: 'end_turn',
        usage: { inputTokens: 1, outputTokens: 1 },
      });
      finish?.();
    });

    expect(signal?.aborted).toBe(true);
    expect(screen.getByText('New answer')).toBeInTheDocument();
    expect(screen.queryByText(/OLD RESPONSE/)).not.toBeInTheDocument();
    expect(callbacks.onSession).not.toHaveBeenCalled();
    expect(callbacks.onCompleted).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument();
  });

  it('does not start a request when navigation cancels pending token retrieval', async () => {
    let finishToken: ((token: string) => void) | undefined;
    mocks.getAccessToken.mockImplementation(
      () =>
        new Promise<string>((resolve) => {
          finishToken = resolve;
        }),
    );
    const props = { config, onSession: vi.fn(), onCompleted: vi.fn() };
    const view = render(
      <ChatPanel {...props} initialMessages={initialMessages} />,
    );
    fireEvent.change(screen.getByRole('textbox'), {
      target: { value: 'Old question' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    view.rerender(<ChatPanel {...props} initialMessages={[]} />);

    await act(async () => {
      finishToken?.('token');
    });

    expect(mocks.streamChat).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument();
  });

  it('aborts on unmount and ignores late errors and callbacks', async () => {
    let emit: ((event: SseEvent) => void) | undefined;
    let reject: ((error: Error) => void) | undefined;
    let signal: AbortSignal | undefined;
    mocks.getAccessToken.mockResolvedValue('token');
    mocks.streamChat.mockImplementation(
      (options: {
        onEvent: (event: SseEvent) => void;
        signal: AbortSignal;
      }) => {
        emit = options.onEvent;
        signal = options.signal;
        return new Promise<void>((_resolve, rejectRequest) => {
          reject = rejectRequest;
        });
      },
    );
    const onSession = vi.fn();
    const view = render(
      <ChatPanel
        config={config}
        initialMessages={initialMessages}
        onSession={onSession}
        onCompleted={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByRole('textbox'), {
      target: { value: 'Old question' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() => expect(emit).toBeDefined());
    view.unmount();

    await act(async () => {
      emit?.({ type: 'session', sessionId: 'old' });
      reject?.(new Error('late network error'));
    });

    expect(signal?.aborted).toBe(true);
    expect(onSession).not.toHaveBeenCalled();
  });
});

describe('ChatPanel message formatting', () => {
  it('formats assistant Markdown but shows user text exactly as typed', () => {
    const createdAt = '2026-10-05T00:00:00.000Z';
    render(
      <ChatPanel
        config={config}
        initialMessages={[
          {
            role: 'user',
            content: 'what is **this**',
            citations: [],
            createdAt,
          },
          {
            role: 'assistant',
            content: 'It is **bold**',
            citations: [],
            createdAt,
          },
        ]}
        onSession={vi.fn()}
        onCompleted={vi.fn()}
      />,
    );

    expect(screen.getByText('what is **this**')).toBeInTheDocument();
    expect(screen.getByText('bold').tagName).toBe('STRONG');
  });
});
