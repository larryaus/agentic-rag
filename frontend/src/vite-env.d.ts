/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_OIDC_ISSUER: string;
  readonly VITE_OIDC_CLIENT_ID: string;
  readonly VITE_OIDC_AUTHORIZE_URL: string;
  readonly VITE_OIDC_TOKEN_URL: string;
  readonly VITE_OIDC_JWKS_URL: string;
  readonly VITE_OIDC_SCOPE: string;
  readonly VITE_OIDC_LOGOUT_URL: string;
  readonly VITE_OIDC_LOGOUT_MODE: string;
  readonly VITE_API_URL: string;
  readonly VITE_CHAT_URL: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
