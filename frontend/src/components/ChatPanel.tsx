import type { Citation, MessageView, SseEvent } from '@kb/shared';
import { useEffect, useRef, useState } from 'react';

import type { AppConfig } from '../config';
import { getAccessToken } from '../auth/auth';
import { apiFetch } from '../api/http';
import { streamChat } from '../api/sse';
import { CitationText } from './CitationText';

type ChatMessage = Pick<MessageView, 'role' | 'content' | 'citations'>;

export function ChatPanel(props: {
  config: AppConfig;
  sessionId?: string;
  initialMessages: MessageView[];
  onSession: (sessionId: string) => void;
  onCompleted: () => void;
}): React.JSX.Element {
  const [messages, setMessages] = useState<ChatMessage[]>(props.initialMessages);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [status, setStatus] = useState('');
  const currentCitations = useRef<Citation[]>([]);

  useEffect(() => {
    setMessages(props.initialMessages);
  }, [props.initialMessages]);

  const openCitation = async (citation: Citation): Promise<void> => {
    const response = await apiFetch(
      props.config,
      `/v1/documents/${encodeURIComponent(citation.documentId)}/download`,
    );
    const payload = (await response.json()) as { url: string };
    window.open(payload.url, '_blank', 'noopener,noreferrer');
  };

  const handleEvent = (event: SseEvent): void => {
    if (event.type === 'session') {
      props.onSession(event.sessionId);
    } else if (event.type === 'tool_use') {
      setStatus(`Searching for “${String(event.input.query ?? '')}”…`);
    } else if (event.type === 'citation') {
      currentCitations.current.push({
        ref: event.ref,
        title: event.title,
        documentId: event.documentId,
        score: event.score,
        snippet: event.snippet,
      });
    } else if (event.type === 'text') {
      setStatus('');
      setMessages((current) => {
        const next = [...current];
        const last = next.at(-1);
        if (last?.role === 'assistant') {
          next[next.length - 1] = {
            ...last,
            content: last.content + event.delta,
            citations: [...currentCitations.current],
          };
        } else {
          next.push({
            role: 'assistant',
            content: event.delta,
            citations: [...currentCitations.current],
          });
        }
        return next;
      });
    } else if (event.type === 'error') {
      setStatus(event.message);
    } else if (event.type === 'done') {
      setStatus('');
      props.onCompleted();
    }
  };

  const submit = async (): Promise<void> => {
    const message = input.trim();
    if (message === '' || streaming) return;
    setInput('');
    setMessages((current) => [
      ...current,
      { role: 'user', content: message, citations: [] },
    ]);
    setStreaming(true);
    setStatus('Thinking…');
    currentCitations.current = [];
    try {
      const token = await getAccessToken(props.config);
      await streamChat({
        url: props.config.chatUrl,
        accessToken: token,
        ...(props.sessionId === undefined
          ? {}
          : { sessionId: props.sessionId }),
        message,
        onEvent: handleEvent,
      });
    } catch (cause) {
      setStatus(
        cause instanceof Error ? cause.message : 'The chat request failed',
      );
    } finally {
      setStreaming(false);
    }
  };

  return (
    <section className="panel chat-panel">
      <div className="messages" aria-live="polite">
        {messages.length === 0 ? (
          <div className="empty-state">
            <p className="eyebrow">Grounded answers</p>
            <h2>Ask your company knowledge base</h2>
            <p>Answers stream in with links to the supporting source files.</p>
          </div>
        ) : (
          messages.map((message, index) => (
            <article
              key={`${message.role}-${index}`}
              className={`message ${message.role}`}
            >
              <span className="message-label">
                {message.role === 'user' ? 'You' : 'Assistant'}
              </span>
              <p>
                <CitationText
                  text={message.content}
                  citations={message.citations}
                  onOpen={(citation) => void openCitation(citation)}
                />
              </p>
            </article>
          ))
        )}
        {status === '' ? null : <p className="stream-status">{status}</p>}
      </div>
      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <textarea
          value={input}
          maxLength={8000}
          placeholder="Ask a question about company documents…"
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault();
              void submit();
            }
          }}
        />
        <button type="submit" disabled={streaming || input.trim() === ''}>
          {streaming ? 'Working…' : 'Send'}
        </button>
      </form>
    </section>
  );
}
