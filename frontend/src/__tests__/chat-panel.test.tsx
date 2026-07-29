// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { MessageView } from '@kb/shared';
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
    mocks.getAccessToken.mockResolvedValue('token');
    mocks.streamChat.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          finishStream = resolve;
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

    view.rerender(
      <ChatPanel
        {...baseProps}
        sessionId="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
      />,
    );

    expect(screen.getByText('First question')).toBeInTheDocument();
    finishStream?.();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument(),
    );
  });
});
