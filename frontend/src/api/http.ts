import type { AppConfig } from '../config';
import { getAccessToken } from '../auth/auth';

export async function apiFetch(
  config: AppConfig,
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const token = await getAccessToken(config);
  const headers = new Headers(init.headers);
  headers.set('authorization', `Bearer ${token}`);
  if (init.body !== undefined && !headers.has('content-type')) {
    headers.set('content-type', 'application/json');
  }
  const response = await fetch(`${config.apiUrl}${path}`, {
    ...init,
    headers,
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const payload = (await response.json()) as { message?: unknown };
      if (typeof payload.message === 'string') message = payload.message;
    } catch {
      // The status remains useful when an intermediary returns non-JSON.
    }
    throw new Error(message);
  }
  return response;
}
