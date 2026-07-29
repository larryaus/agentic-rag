import type { MessageView, SessionDetail } from '@kb/shared';
import { useState } from 'react';

import type { AppConfig } from '../config';
import { apiFetch } from '../api/http';
import { logout } from '../auth/auth';
import { ChatPanel } from './ChatPanel';
import { DocumentPanel } from './DocumentPanel';
import { SessionList } from './SessionList';

export function AppShell(props: {
  config: AppConfig;
}): React.JSX.Element {
  const [sessionId, setSessionId] = useState<string>();
  const [messages, setMessages] = useState<MessageView[]>([]);
  const [sessionVersion, setSessionVersion] = useState(0);

  const selectSession = async (id: string): Promise<void> => {
    const response = await apiFetch(props.config, `/v1/sessions/${id}`);
    const detail = (await response.json()) as SessionDetail;
    setSessionId(id);
    setMessages(detail.messages);
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
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
          onSelect={(id) => void selectSession(id)}
          onNew={() => {
            setSessionId(undefined);
            setMessages([]);
          }}
        />
        <DocumentPanel config={props.config} />
      </aside>
      <main>
        <ChatPanel
          config={props.config}
          {...(sessionId === undefined ? {} : { sessionId })}
          initialMessages={messages}
          onSession={setSessionId}
          onCompleted={() => setSessionVersion((value) => value + 1)}
        />
      </main>
    </div>
  );
}
