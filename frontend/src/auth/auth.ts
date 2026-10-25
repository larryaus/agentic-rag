/**
 * Authorization code + PKCE is paired with state and a signature-verified nonce claim.
 * Works with any OpenID Connect provider; Cognito differs only in its logout parameters.
 */
import { JwtVerifier } from 'aws-jwt-verify';

import type { AppConfig } from '../config';

const VERIFIER_KEY = 'kb.auth.codeVerifier';
const STATE_KEY = 'kb.auth.state';
const NONCE_KEY = 'kb.auth.nonce';
const TOKENS_KEY = 'kb.auth.tokens';

export type TokenSet = {
  accessToken: string;
  idToken: string;
  refreshToken: string;
  expiresAt: number;
};

type TokenResponse = {
  access_token?: unknown;
  id_token?: unknown;
  refresh_token?: unknown;
  expires_in?: unknown;
};

function base64Url(bytes: Uint8Array): string {
  let binary = '';
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return btoa(binary)
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');
}

function randomValue(bytes = 32): string {
  const data = new Uint8Array(bytes);
  crypto.getRandomValues(data);
  return base64Url(data);
}

async function challenge(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(verifier),
  );
  return base64Url(new Uint8Array(digest));
}

function redirectUri(): string {
  return `${window.location.origin}/callback`;
}

function parseTokenResponse(
  payload: TokenResponse,
  existingRefreshToken = '',
): TokenSet {
  if (
    typeof payload.access_token !== 'string' ||
    typeof payload.id_token !== 'string' ||
    typeof payload.expires_in !== 'number'
  ) {
    throw new Error('The identity provider returned an invalid token response');
  }
  const refreshToken =
    typeof payload.refresh_token === 'string'
      ? payload.refresh_token
      : existingRefreshToken;
  return {
    accessToken: payload.access_token,
    idToken: payload.id_token,
    refreshToken,
    expiresAt: Date.now() + payload.expires_in * 1000,
  };
}

async function tokenRequest(
  config: AppConfig,
  params: URLSearchParams,
): Promise<TokenResponse> {
  const response = await fetch(config.oidcTokenUrl, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: params,
  });
  if (!response.ok) {
    throw new Error('Token exchange failed');
  }
  return (await response.json()) as TokenResponse;
}

export async function beginLogin(config: AppConfig): Promise<void> {
  const codeVerifier = randomValue(64);
  const state = randomValue();
  const nonce = randomValue();
  sessionStorage.setItem(VERIFIER_KEY, codeVerifier);
  sessionStorage.setItem(STATE_KEY, state);
  sessionStorage.setItem(NONCE_KEY, nonce);

  const params = new URLSearchParams({
    response_type: 'code',
    client_id: config.oidcClientId,
    redirect_uri: redirectUri(),
    scope: config.oidcScope,
    code_challenge_method: 'S256',
    code_challenge: await challenge(codeVerifier),
    state,
    nonce,
  });
  window.location.assign(
    `${config.oidcAuthorizeUrl}?${params.toString()}`,
  );
}

async function verifyCallbackIdToken(
  config: AppConfig,
  idToken: string,
  nonce: string,
): Promise<void> {
  const verifier = JwtVerifier.create({
    issuer: config.oidcIssuer,
    audience: config.oidcClientId,
    jwksUri: config.oidcJwksUrl,
  });
  await verifier.verify(idToken, {
    customJwtCheck: ({ payload }) => {
      if (payload.nonce !== nonce) {
        throw new Error('ID token nonce mismatch');
      }
    },
  });
}

export async function completeCallback(config: AppConfig): Promise<TokenSet> {
  const params = new URLSearchParams(window.location.search);
  const code = params.get('code');
  const returnedState = params.get('state');
  const expectedState = sessionStorage.getItem(STATE_KEY);
  const codeVerifier = sessionStorage.getItem(VERIFIER_KEY);
  const nonce = sessionStorage.getItem(NONCE_KEY);
  try {
    if (
      code === null ||
      returnedState === null ||
      expectedState === null ||
      codeVerifier === null ||
      nonce === null ||
      returnedState !== expectedState
    ) {
      throw new Error('Invalid authorization callback');
    }
    const response = await tokenRequest(
      config,
      new URLSearchParams({
        grant_type: 'authorization_code',
        client_id: config.oidcClientId,
        code,
        redirect_uri: redirectUri(),
        code_verifier: codeVerifier,
      }),
    );
    const tokens = parseTokenResponse(response);
    await verifyCallbackIdToken(config, tokens.idToken, nonce);
    sessionStorage.setItem(TOKENS_KEY, JSON.stringify(tokens));
    return tokens;
  } finally {
    sessionStorage.removeItem(VERIFIER_KEY);
    sessionStorage.removeItem(STATE_KEY);
    sessionStorage.removeItem(NONCE_KEY);
  }
}

export function readTokens(): TokenSet | undefined {
  const raw = sessionStorage.getItem(TOKENS_KEY);
  if (raw === null) return undefined;
  try {
    const value = JSON.parse(raw) as Partial<TokenSet>;
    if (
      typeof value.accessToken !== 'string' ||
      typeof value.idToken !== 'string' ||
      typeof value.refreshToken !== 'string' ||
      typeof value.expiresAt !== 'number'
    ) {
      return undefined;
    }
    return value as TokenSet;
  } catch {
    return undefined;
  }
}

export async function getAccessToken(config: AppConfig): Promise<string> {
  const tokens = readTokens();
  if (tokens === undefined) {
    throw new Error('Not signed in');
  }
  if (tokens.expiresAt - Date.now() > 5 * 60 * 1000) {
    return tokens.accessToken;
  }
  if (tokens.refreshToken === '') {
    clearTokens();
    throw new Error('Session expired');
  }
  const response = await tokenRequest(
    config,
    new URLSearchParams({
      grant_type: 'refresh_token',
      client_id: config.oidcClientId,
      refresh_token: tokens.refreshToken,
    }),
  );
  const refreshed = parseTokenResponse(response, tokens.refreshToken);
  sessionStorage.setItem(TOKENS_KEY, JSON.stringify(refreshed));
  return refreshed.accessToken;
}

export function clearTokens(): void {
  sessionStorage.removeItem(TOKENS_KEY);
}

export function logout(config: AppConfig): void {
  const idToken = readTokens()?.idToken;
  clearTokens();
  const params = new URLSearchParams({ client_id: config.oidcClientId });
  if (config.oidcLogoutMode === 'cognito') {
    params.set('logout_uri', window.location.origin);
  } else {
    params.set('post_logout_redirect_uri', window.location.origin);
    // The hint lets a standard provider end the session without asking to confirm.
    if (idToken !== undefined) params.set('id_token_hint', idToken);
  }
  window.location.assign(`${config.oidcLogoutUrl}?${params.toString()}`);
}
