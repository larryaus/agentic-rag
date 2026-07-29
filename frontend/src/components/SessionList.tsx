import type { Page, SessionSummary } from '@kb/shared';
import { useCallback, useEffect, useState } from 'react';

import type { AppConfig } from '../config';
import { apiFetch } from '../api/http';

export function SessionList(props: {
  config: AppConfig;
  activeSessionId?: string;
  refreshVersion: number;
  onSelect: (sessionId: string) => void;
  onNew: () => void;
}): React.JSX.Element {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);

  const refresh = useCallback(async () => {
    const response = await apiFetch(props.config, '/v1/sessions');
    const page = (await response.json()) as Page<SessionSummary>;
    setSessions(page.items);
  }, [props.config]);

  useEffect(() => {
    void refresh();
  }, [refresh, props.refreshVersion]);

  return (
    <nav className="session-nav" aria-label="Conversation history">
      <button type="button" className="new-chat" onClick={props.onNew}>
        + New conversation
      </button>
      <ul>
        {sessions.map((session) => (
          <li key={session.sessionId}>
            <button
              type="button"
              className={
                session.sessionId === props.activeSessionId ? 'active' : ''
              }
              onClick={() => props.onSelect(session.sessionId)}
            >
              <span>{session.title}</span>
              <small>{new Date(session.updatedAt).toLocaleDateString()}</small>
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}
