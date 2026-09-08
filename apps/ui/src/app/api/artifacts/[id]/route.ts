import {
  backendUnavailableResponse,
  getCodeExecutorUrl,
} from '@/lib/backend-env';
import { PANEL_CSP } from '@/lib/panel-csp';

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$/.test(id) || id.includes('..')) {
    return new Response('Invalid artifact id', { status: 400 });
  }
  const executorUrl = getCodeExecutorUrl();
  const internalApiKey = process.env.INTERNAL_API_KEY || '';

  let res: Response;
  try {
    res = await fetch(`${executorUrl}/artifacts/${encodeURIComponent(id)}`, {
      cache: 'no-store',
      signal: _req.signal,
      headers: {
        ...(internalApiKey
          ? { Authorization: `Bearer ${internalApiKey}` }
          : {}),
      },
    });
  } catch (error) {
    return backendUnavailableResponse('Code executor', executorUrl, error);
  }
  if (!res.ok) {
    await res.body?.cancel();
    return new Response('Artifact not found or expired', {
      status: res.status,
      headers: {
        'cache-control': 'private, no-store, max-age=0',
      },
    });
  }

  const contentType =
    res.headers.get('content-type') || 'application/octet-stream';
  const headers: Record<string, string> = {
    'content-type': contentType,
    'cache-control': 'private, no-store, max-age=0',
    pragma: 'no-cache',
    'x-content-type-options': 'nosniff',
  };
  for (const name of [
    'content-disposition',
    'x-artifact-sha256',
    'x-artifact-expires-at',
  ]) {
    const value = res.headers.get(name);
    if (value) headers[name] = value;
  }
  // Generated HTML panels run untrusted code under the shared panel CSP.
  if (contentType.includes('text/html')) {
    headers['content-security-policy'] = PANEL_CSP;
  }

  return new Response(res.body, { headers });
}
