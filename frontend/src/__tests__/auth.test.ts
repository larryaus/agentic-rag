// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { beginLogin, getAccessToken, logout, readTokens } from '../auth/auth';
import type { AppConfig } from '../config';

const config: AppConfig = {
  oidcIssuer: 'http://localhost:8080/realms/kb',
  oidcClientId: 'kb-web',
  oidcAuthorizeUrl: 'http://localhost:8080/realms/kb/protocol/openid-connect/auth',
  oidcTokenUrl: 'http://localhost:8080/realms/kb/protocol/openid-connect/token',
  oidcJwksUrl: 'http://localhost:8080/realms/kb/protocol/openid-connect/certs',
  oidcScope: 'openid profile email kb-api',
  oidcLogoutUrl: 'http://localhost:8080/realms/kb/protocol/openid-connect/logout',
  oidcLogoutMode: 'oidc',
  apiUrl: 'http://localhost:8000',
  chatUrl: 'http://localhost:8000/v1/chat',
};

const original = window.location;
let assign: ReturnType<typeof vi.fn>;

beforeEach(() => {
  assign = vi.fn();
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { origin: 'http://localhost:8000', search: '', assign },
  });
});

afterEach(() => {
  Object.defineProperty(window, 'location', { configurable: true, value: original });
  sessionStorage.clear();
  localStorage.clear();
  vi.unstubAllGlobals();
});

function target(): URL {
  return new URL(assign.mock.calls[0]?.[0] as string);
}

describe('generic OIDC sign-in', () => {
  it('sends the configured client and scope to the authorize endpoint with PKCE', async () => {
    await beginLogin(config);
    const url = target();
    expect(`${url.origin}${url.pathname}`).toBe(config.oidcAuthorizeUrl);
    expect(url.searchParams.get('client_id')).toBe('kb-web');
    expect(url.searchParams.get('scope')).toBe('openid profile email kb-api');
    expect(url.searchParams.get('redirect_uri')).toBe('http://localhost:8000/callback');
    expect(url.searchParams.get('code_challenge_method')).toBe('S256');
    expect(url.searchParams.get('state')).toBe(sessionStorage.getItem('kb.auth.state'));
  });

  it('logs out with the standard parameters for OIDC providers', () => {
    localStorage.setItem(
      'kb.auth.tokens',
      JSON.stringify({ accessToken: 'a', idToken: 'id-token', refreshToken: 'r', expiresAt: 1 }),
    );
    logout(config);
    const url = target();
    expect(`${url.origin}${url.pathname}`).toBe(config.oidcLogoutUrl);
    expect(url.searchParams.get('client_id')).toBe('kb-web');
    expect(url.searchParams.get('post_logout_redirect_uri')).toBe('http://localhost:8000');
    expect(url.searchParams.get('id_token_hint')).toBe('id-token');
    expect(url.searchParams.has('logout_uri')).toBe(false);
    expect(localStorage.getItem('kb.auth.tokens')).toBeNull();
  });

  it("logs out with Cognito's own parameter in cognito mode", () => {
    logout({ ...config, oidcLogoutMode: 'cognito', oidcLogoutUrl: 'https://kb.auth.test/logout' });
    const url = target();
    expect(url.searchParams.get('logout_uri')).toBe('http://localhost:8000');
    expect(url.searchParams.has('post_logout_redirect_uri')).toBe(false);
    expect(url.searchParams.has('id_token_hint')).toBe(false);
  });

  const expired = { accessToken: 'a', idToken: 'i', refreshToken: 'r', expiresAt: 1 };

  it('refreshes an expired access token and keeps the session in localStorage', async () => {
    localStorage.setItem('kb.auth.tokens', JSON.stringify(expired));
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        Response.json({ access_token: 'a2', id_token: 'i2', expires_in: 3600 }),
      ),
    );
    await expect(getAccessToken(config)).resolves.toBe('a2');
    expect(readTokens()).toMatchObject({ accessToken: 'a2', refreshToken: 'r' });
    expect(sessionStorage.getItem('kb.auth.tokens')).toBeNull();
  });

  it('returns to sign-in when the provider rejects the stored refresh token', async () => {
    localStorage.setItem('kb.auth.tokens', JSON.stringify(expired));
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 400 })));
    await expect(getAccessToken(config)).rejects.toThrow('Session expired');
    expect(readTokens()).toBeUndefined();
    expect(assign).toHaveBeenCalledWith('http://localhost:8000');
  });

  it('keeps the session when a refresh fails for another reason', async () => {
    localStorage.setItem('kb.auth.tokens', JSON.stringify(expired));
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 503 })));
    await expect(getAccessToken(config)).rejects.toThrow('Token exchange failed');
    expect(readTokens()).toMatchObject({ refreshToken: 'r' });
  });
});
