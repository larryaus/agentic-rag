/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_USER_POOL_ID: string;
  readonly VITE_USER_POOL_CLIENT_ID: string;
  readonly VITE_COGNITO_DOMAIN: string;
  readonly VITE_API_URL: string;
  readonly VITE_CHAT_URL: string;
  readonly VITE_AWS_REGION: string;
  /** Local development only; see tools/mock-server.mjs. Unset in deployed builds. */
  readonly VITE_MOCK_JWKS_URI?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
