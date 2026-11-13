import type { MessageView, SessionDetail } from '@kb/shared';
import { useRef, useState } from 'react';

import type { AppConfig } from '../config';
import { apiFetch } from '../api/http';
import { logout } from '../auth/auth';
import { ChatPanel } from './ChatPanel';
import { DocumentPanel } from './DocumentPanel';
import { SessionList } from './SessionList';

export function AppShell(props: { config: AppConfig }): React.JSX.Element {
  const [sessionId, setSessionId] = useState<string>();
  const [messages, setMessages] = useState<MessageView[]>([]);
  const [sessionVersion, setSessionVersion] = useState(0);
  const [conversationVersion, setConversationVersion] = useState(0);
  const [loadingSession, setLoadingSession] = useState(false);
  const [sessionError, setSessionError] = useState('');
  // Phone-width layouts keep the sidebar in a drawer; wider ones ignore this.
  const [menuOpen, setMenuOpen] = useState(false);
  const selectionVersion = useRef(0);

  const selectSession = async (id: string): Promise<void> => {
    const selection = ++selectionVersion.current;
    setLoadingSession(true);
    setSessionError('');
    setConversationVersion((value) => value + 1);
    try {
      const response = await apiFetch(props.config, `/v1/sessions/${id}`);
      const detail = (await response.json()) as SessionDetail;
      if (selection !== selectionVersion.current) return;
      setSessionId(id);
      setMessages(detail.messages);
    } catch (cause) {
      if (selection !== selectionVersion.current) return;
      setSessionId(undefined);
      setMessages([]);
      setSessionError(
        cause instanceof Error ? cause.message : 'Could not load conversation',
      );
    } finally {
      if (selection === selectionVersion.current) setLoadingSession(false);
    }
  };

  return (
    <div className={menuOpen ? 'app-shell menu-open' : 'app-shell'}>
      <header className="topbar">
        <div>
          <button
            type="button"
            className="menu-button"
            aria-label="Conversations and documents"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((open) => !open)}
          >
            <span aria-hidden="true">☰</span>
          </button>
          <span className="brand-mark">KB</span>
          <strong>Enterprise Knowledge Base</strong>
        </div>
        <button type="button" onClick={() => logout(props.config)}>
          Sign out
        </button>
      </header>
      <aside>
        <SessionList
          config={props.config}
          {...(sessionId === undefined ? {} : { activeSessionId: sessionId })}
          refreshVersion={sessionVersion}
          onSelect={(id) => {
            setMenuOpen(false);
            void selectSession(id);
          }}
          onNew={() => {
            setMenuOpen(false);
            selectionVersion.current += 1;
            setLoadingSession(false);
            setSessionError('');
            setConversationVersion((value) => value + 1);
            setSessionId(undefined);
            setMessages([]);
          }}
        />
        <DocumentPanel config={props.config} />
      </aside>
      <button
        type="button"
        className="drawer-backdrop"
        aria-label="Close menu"
        tabIndex={-1}
        onClick={() => setMenuOpen(false)}
      />
      <main>
        {sessionError === '' ? null : (
          <p role="alert" className="error-banner">
            {sessionError}
          </p>
        )}
        {loadingSession ? (
          <p role="status">Loading conversation…</p>
        ) : (
          <ChatPanel
            key={conversationVersion}
            config={props.config}
            {...(sessionId === undefined ? {} : { sessionId })}
            initialMessages={messages}
            onSession={setSessionId}
            onCompleted={() => setSessionVersion((value) => value + 1)}
          />
        )}
      </main>
    </div>
  );
}
