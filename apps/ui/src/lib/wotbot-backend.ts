import { backendUnavailableResponse, getWotbotUrl } from '@/lib/backend-env';
import { workspaceHeaders } from '@/lib/workspace';

const internalApiKey = process.env.INTERNAL_API_KEY || '';

async function buildHeaders(headers?: HeadersInit) {
  const merged = new Headers(headers);
  if (internalApiKey) {
    merged.set('Authorization', `Bearer ${internalApiKey}`);
  }
  for (const [name, value] of Object.entries(await workspaceHeaders())) {
    merged.set(name, value);
  }
  return merged;
}

export async function fetchWotbot(path: string, init: RequestInit = {}) {
  const wotbotUrl = getWotbotUrl();

  try {
    return await fetch(`${wotbotUrl}${path}`, {
      ...init,
      cache: init.cache ?? 'no-store',
      headers: await buildHeaders(init.headers),
    });
  } catch (error) {
    if (init.signal?.aborted) {
      throw error;
    }
    return backendUnavailableResponse('WoTBot backend', wotbotUrl, error);
  }
}

export async function proxyWotbotJson(path: string, init: RequestInit = {}) {
  const response = await fetchWotbot(path, init);
  const body = await response.text();
  return new Response(body, {
    status: response.status,
    headers: {
      'Content-Type':
        response.headers.get('content-type') || 'application/json',
    },
  });
}
