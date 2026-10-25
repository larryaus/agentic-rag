export type LogoutMode = 'cognito' | 'oidc';

/**
 * Any OpenID Connect provider: Cognito on AWS, Keycloak in the local deployment. Cognito's
 * logout endpoint predates the standard, so the logout parameters depend on the mode.
 */
export type AppConfig = {
  oidcIssuer: string;
  oidcClientId: string;
  oidcAuthorizeUrl: string;
  oidcTokenUrl: string;
  oidcJwksUrl: string;
  oidcScope: string;
  oidcLogoutUrl: string;
  oidcLogoutMode: LogoutMode;
  apiUrl: string;
  chatUrl: string;
};

function required(name: keyof ImportMetaEnv): string {
  const value = import.meta.env[name];
  if (value === undefined || value.trim() === '') {
    throw new Error(`Missing frontend environment variable: ${name}`);
  }
  return value.replace(/\/$/, '');
}

function logoutMode(): LogoutMode {
  const value = required('VITE_OIDC_LOGOUT_MODE');
  if (value !== 'cognito' && value !== 'oidc') {
    throw new Error('VITE_OIDC_LOGOUT_MODE must be cognito or oidc');
  }
  return value;
}

export function loadConfig(): AppConfig {
  return {
    oidcIssuer: required('VITE_OIDC_ISSUER'),
    oidcClientId: required('VITE_OIDC_CLIENT_ID'),
    oidcAuthorizeUrl: required('VITE_OIDC_AUTHORIZE_URL'),
    oidcTokenUrl: required('VITE_OIDC_TOKEN_URL'),
    oidcJwksUrl: required('VITE_OIDC_JWKS_URL'),
    oidcScope: required('VITE_OIDC_SCOPE'),
    oidcLogoutUrl: required('VITE_OIDC_LOGOUT_URL'),
    oidcLogoutMode: logoutMode(),
    apiUrl: required('VITE_API_URL'),
    chatUrl: required('VITE_CHAT_URL'),
  };
}
