import { httpClient, httpJson } from '@/lib/http-client';

export interface ProviderSchema {
  provider: string;
  title: string;
  capabilities: string[];
  config_schema: {
    properties?: Record<
      string,
      { default?: string | number; format?: string; type?: string }
    >;
    required?: string[];
  };
  default_security_scheme: string;
  security_schemes: string[];
}

export interface DiscoverySource {
  source_id: string;
  provider: string;
  capabilities: string[];
  external_id: string;
  title: string;
  description: string;
  tags: string[];
  config: Record<string, string | number>;
  network_access: 'public' | 'private';
  security_name: string;
  security_scheme: string;
  credential_status: 'not_required' | 'required' | 'configured';
  dependent_thing_count: number;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface SourceCredentialChallenge {
  status: 'credential_required' | 'credential_rejected';
  owner_kind: 'source';
  source_id: string;
  security_name: string;
  scheme: string;
}

export interface SourceOnboardingResult {
  created?: boolean;
  thing?: { id: string; title: string };
  refresh_available?: boolean;
  warnings?: string[];
  status?:
    | 'selection_required'
    | 'no_supported_things'
    | 'permission_required'
    | 'onboarding_failed';
  message?: string;
}

export interface SourceRegistrationResult {
  created?: boolean;
  source?: DiscoverySource;
  credential_challenge?: SourceCredentialChallenge;
  unsupported_source?: boolean;
  probe_evidence?: string[];
  onboarding?: SourceOnboardingResult;
}

export interface SourceDraft {
  url?: string;
  provider?: string;
  title?: string;
  description?: string;
  tags?: string[];
  config?: Record<string, unknown>;
  security_scheme?: string;
}

export async function fetchProviderSchemas(): Promise<ProviderSchema[]> {
  return (await httpJson<{ items: ProviderSchema[] }>('/discovery/providers'))
    .items;
}

export async function fetchSources(
  page: number,
  perPage: number,
  search: string,
): Promise<{ data: DiscoverySource[]; total: number }> {
  const query = new URLSearchParams({
    page: String(page),
    per_page: String(perPage),
  });
  if (search.trim()) query.set('q', search.trim());
  const result = await httpJson<{
    items: DiscoverySource[];
    total: number;
  }>(`/discovery/sources?${query.toString()}`);
  return { data: result.items, total: result.total };
}

export async function fetchSource(sourceId: string): Promise<DiscoverySource> {
  return httpJson(`/discovery/sources/${encodeURIComponent(sourceId)}`);
}

export async function registerDetectedSource(
  url: string,
  networkAccess: 'public' | 'private',
): Promise<SourceRegistrationResult> {
  return sourceRegistrationRequest('/discovery/sources/detect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, network_access: networkAccess }),
  });
}

export async function saveSource(
  payload: {
    provider: string;
    title: string;
    description: string;
    tags: string[];
    config: Record<string, string | number>;
    security: { name: string; scheme: string };
    network_access: 'public' | 'private';
  },
  sourceId?: string,
): Promise<SourceRegistrationResult> {
  return sourceRegistrationRequest(
    sourceId
      ? `/discovery/sources/${encodeURIComponent(sourceId)}`
      : '/discovery/sources',
    {
      method: sourceId ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  );
}

async function sourceRegistrationRequest(
  path: string,
  options: RequestInit,
): Promise<SourceRegistrationResult> {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: {
      Accept: 'application/json',
      ...(options.headers || {}),
    },
  });
  const body = (await response.json().catch(() => ({}))) as Record<
    string,
    unknown
  >;
  if (response.status === 428) {
    const detail = body.detail;
    if (
      !detail ||
      typeof detail !== 'object' ||
      typeof (detail as Record<string, unknown>).source_id !== 'string'
    ) {
      throw new Error(
        'Source requires credentials but returned no safe challenge',
      );
    }
    const challenge = detail as unknown as SourceCredentialChallenge;
    return {
      source: await fetchSource(challenge.source_id),
      credential_challenge: challenge,
    };
  }
  if (!response.ok) {
    const detail = body.detail;
    throw new Error(
      typeof detail === 'string'
        ? detail
        : `Source request failed (${response.status})`,
    );
  }
  return body as unknown as SourceRegistrationResult;
}

export async function deleteSource(sourceId: string): Promise<void> {
  await httpClient(`/discovery/sources/${encodeURIComponent(sourceId)}`, {
    method: 'DELETE',
  });
}

export interface ThingRefreshDiff {
  added_actions: string[];
  removed_actions: string[];
  changed_actions: string[];
  metadata_changed: boolean;
  server_changed: boolean;
  security_changed: boolean;
}

export interface ThingRefreshPreview {
  refresh_id: string;
  expires_in_seconds: number;
  thing_id: string;
  diff: ThingRefreshDiff;
  warnings: string[];
}

export async function previewThingRefresh(
  thingId: string,
): Promise<ThingRefreshPreview> {
  return httpJson(
    `/discovery/things/${encodeURIComponent(thingId)}/refresh/preview`,
    { method: 'POST' },
  );
}

export async function applyThingRefresh(
  thingId: string,
  refreshId: string,
): Promise<void> {
  await httpJson(`/discovery/things/${encodeURIComponent(thingId)}/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_id: refreshId }),
  });
}
