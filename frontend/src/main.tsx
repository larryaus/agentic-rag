import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './App';
import { clearTokens, completeCallback } from './auth/auth';
import { loadConfig } from './config';
import './styles.css';

async function start(): Promise<void> {
  let authError: string | undefined;
  if (window.location.pathname === '/callback') {
    try {
      await completeCallback(loadConfig());
    } catch (cause) {
      clearTokens();
      authError =
        cause instanceof Error ? cause.message : 'Authentication failed';
    } finally {
      window.history.replaceState({}, '', '/');
    }
  }
  const root = document.getElementById('root');
  if (root === null) throw new Error('Missing application root');
  createRoot(root).render(
    <StrictMode>
      <App {...(authError === undefined ? {} : { authError })} />
    </StrictMode>,
  );
}

void start();
