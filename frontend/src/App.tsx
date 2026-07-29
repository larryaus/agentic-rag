import { useMemo } from 'react';

import { readTokens } from './auth/auth';
import { loadConfig } from './config';
import { AppShell } from './components/AppShell';
import { LoginScreen } from './components/LoginScreen';

export function App(props: { authError?: string }): React.JSX.Element {
  const config = useMemo(loadConfig, []);
  const tokens = readTokens();
  if (tokens === undefined) {
    return (
      <LoginScreen
        config={config}
        {...(props.authError === undefined ? {} : { error: props.authError })}
      />
    );
  }
  return <AppShell config={config} />;
}
