import type { AppConfig } from '../config';
import { beginLogin } from '../auth/auth';

export function LoginScreen(props: {
  config: AppConfig;
  error?: string;
}): React.JSX.Element {
  return (
    <main className="login-screen">
      <section className="login-card">
        <p className="eyebrow">Enterprise AI</p>
        <h1>Knowledge Base Assistant</h1>
        <p>
          Search shared company documents and keep your conversations private.
        </p>
        {props.error === undefined ? null : (
          <p className="error-banner">{props.error}</p>
        )}
        <button
          type="button"
          className="primary-button"
          onClick={() => void beginLogin(props.config)}
        >
          Sign in with Cognito
        </button>
      </section>
    </main>
  );
}
