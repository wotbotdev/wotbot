import { cookies } from 'next/headers';
import { NextRequest, NextResponse } from 'next/server';

import { wotFetch } from '@/lib/wot-api';
import {
  DEFAULT_WORKSPACE_ID,
  WORKSPACE_COOKIE,
  WORKSPACE_HEADER,
} from '@/lib/workspace-selection';

interface WorkspaceItem {
  id: string;
  title: string;
}

// Lists workspaces and the one this browser works in. `enabled` is false when
// the backend runs without workspaces (the route is not mounted there).
export async function GET() {
  const current =
    (await cookies()).get(WORKSPACE_COOKIE)?.value || DEFAULT_WORKSPACE_ID;
  // List without a selected workspace, so a stale cookie cannot hide the list.
  const res = await wotFetch('/workspaces', {
    headers: { [WORKSPACE_HEADER]: '' },
  });
  if (res.status === 404) {
    return NextResponse.json({ enabled: false, current, items: [] });
  }
  const body = await res.json();
  if (!res.ok) {
    return NextResponse.json(body, { status: res.status });
  }
  const items = ((body as { items?: WorkspaceItem[] }).items ?? []).map(
    ({ id, title }) => ({ id, title }),
  );
  return NextResponse.json({ enabled: true, current, items });
}

// Selects the workspace for this browser; later requests forward it.
export async function POST(req: NextRequest) {
  const { id } = (await req.json().catch(() => ({}))) as { id?: unknown };
  if (typeof id !== 'string' || !id.trim()) {
    return NextResponse.json(
      { detail: 'Workspace id is required' },
      { status: 400 },
    );
  }
  const store = await cookies();
  if (id === DEFAULT_WORKSPACE_ID) {
    store.delete(WORKSPACE_COOKIE);
  } else {
    store.set(WORKSPACE_COOKIE, id.trim(), {
      path: '/',
      sameSite: 'lax',
      httpOnly: true,
      maxAge: 60 * 60 * 24 * 365,
    });
  }
  return NextResponse.json({ current: id });
}
