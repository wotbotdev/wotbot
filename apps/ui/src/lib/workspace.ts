import { cookies } from 'next/headers';

import {
  WORKSPACE_COOKIE,
  workspaceHeaderFor,
} from '@/lib/workspace-selection';

/** Header that tells the backend which workspace this request acts in. */
export async function workspaceHeaders(): Promise<Record<string, string>> {
  try {
    const store = await cookies();
    return workspaceHeaderFor(store.get(WORKSPACE_COOKIE)?.value);
  } catch {
    // Called outside a request (no cookies available): use the default workspace.
    return {};
  }
}
