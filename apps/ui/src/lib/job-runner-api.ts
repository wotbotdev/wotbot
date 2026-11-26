import { backendUnavailableResponse, getJobRunnerUrl } from '@/lib/backend-env';
import { workspaceHeaders } from '@/lib/workspace';

export async function jobRunnerFetch(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const base = getJobRunnerUrl();
  const internalApiKey = process.env.INTERNAL_API_KEY || '';

  try {
    return await fetch(`${base}${path}`, {
      ...init,
      headers: {
        Accept: 'application/json',
        ...(internalApiKey
          ? { Authorization: `Bearer ${internalApiKey}` }
          : {}),
        ...(await workspaceHeaders()),
        ...(init.headers instanceof Headers
          ? Object.fromEntries(init.headers.entries())
          : (init.headers ?? {})),
      },
    });
  } catch (error) {
    return backendUnavailableResponse('Job service', base, error);
  }
}
