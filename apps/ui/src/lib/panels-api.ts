import { httpClient, httpJson } from '@/lib/http-client';
import { type WotCapability } from '@/components/wotbot/chat-tool-calls/web-interface-model';

export interface PanelRecord {
  id: string;
  title: string;
  capabilities: WotCapability[];
  data?: Record<string, string>;
  source_thread_id: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface PanelDetail extends PanelRecord {
  html?: string;
}

export interface PanelEditResponse extends PanelRecord {
  browser_validation?: unknown;
}

export interface PanelVersion {
  id: string;
  panel_id: string;
  version: number;
  source: 'initial' | 'manual' | 'ai' | 'restore' | string;
  title: string;
  capabilities: WotCapability[];
  data?: Record<string, string>;
  created_at: string | null;
}

interface PanelListResponse {
  items: PanelRecord[];
}

interface PanelVersionsResponse {
  items: PanelVersion[];
}

export async function fetchPanels(): Promise<PanelRecord[]> {
  const json = await httpJson<PanelListResponse>('/panels');
  return json.items;
}

export async function pinPanel(input: {
  title: string;
  html: string;
  capabilities: WotCapability[];
  data?: Record<string, string>;
  sourceThreadId?: string | null;
}): Promise<PanelRecord> {
  return httpJson<PanelRecord>('/panels', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      title: input.title,
      html: input.html,
      capabilities: input.capabilities,
      data: input.data ?? {},
      source_thread_id: input.sourceThreadId ?? null,
    }),
  });
}

export async function fetchPanelSource(id: string): Promise<PanelDetail> {
  return httpJson<PanelDetail>(
    `/panels/${encodeURIComponent(id)}?include_html=true`,
  );
}

export async function updatePanel(
  id: string,
  patch: {
    title?: string;
    html?: string;
    capabilities?: WotCapability[];
    data?: Record<string, string>;
  },
): Promise<PanelRecord> {
  return httpJson<PanelRecord>(`/panels/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
}

export async function fetchPanelVersions(id: string): Promise<PanelVersion[]> {
  const json = await httpJson<PanelVersionsResponse>(
    `/panels/${encodeURIComponent(id)}/versions`,
  );
  return json.items;
}

export async function restorePanelVersion(
  id: string,
  versionId: string,
): Promise<PanelRecord> {
  return httpJson<PanelRecord>(
    `/panels/${encodeURIComponent(id)}/versions/${encodeURIComponent(versionId)}/restore`,
    { method: 'POST' },
  );
}

export async function editPanel(
  id: string,
  instruction: string,
): Promise<PanelEditResponse> {
  return httpJson<PanelEditResponse>(`/panels/${encodeURIComponent(id)}/edit`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ instruction }),
  });
}

export async function deletePanel(id: string): Promise<void> {
  await httpClient(`/panels/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });
}
