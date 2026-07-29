export type AppConfig = {
  userPoolId: string;
  userPoolClientId: string;
  cognitoDomain: string;
  apiUrl: string;
  chatUrl: string;
  awsRegion: string;
};

function required(name: keyof ImportMetaEnv): string {
  const value = import.meta.env[name];
  if (value === undefined || value.trim() === '') {
    throw new Error(`Missing frontend environment variable: ${name}`);
  }
  return value.replace(/\/$/, '');
}

export function loadConfig(): AppConfig {
  return {
    userPoolId: required('VITE_USER_POOL_ID'),
    userPoolClientId: required('VITE_USER_POOL_CLIENT_ID'),
    cognitoDomain: required('VITE_COGNITO_DOMAIN'),
    apiUrl: required('VITE_API_URL'),
    chatUrl: required('VITE_CHAT_URL'),
    awsRegion: required('VITE_AWS_REGION'),
  };
}
