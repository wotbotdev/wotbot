import { backendUnavailableResponse, getWotApiUrl } from '@/lib/backend-env';
import { workspaceHeaders } from '@/lib/workspace';

export async function wotFetch(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const base = getWotApiUrl();
  const token =
    process.env.WOT_REGISTRY_TOKEN || process.env.INIT_ADMIN_TOKEN || '';
  try {
    return await fetch(`${base}${path}`, {
      ...init,
      headers: {
        Accept: 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(await workspaceHeaders()),
        ...(init.headers instanceof Headers
          ? Object.fromEntries(init.headers.entries())
          : (init.headers ?? {})),
      },
    });
  } catch (error) {
    return backendUnavailableResponse('WoT API', base, error);
  }
}

export async function wotJson<T = unknown>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const res = await wotFetch(path, init);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(
      (body as { detail?: string }).detail || `Request failed (${res.status})`,
    );
  }
  return res.json();
}
