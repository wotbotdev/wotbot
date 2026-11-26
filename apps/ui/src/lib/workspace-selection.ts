// Shared by the browser (workspace switcher) and the server (backend proxies).
// The selected workspace lives in a cookie; server routes forward it to the
// backend as a header. No cookie means the default workspace.

export const WORKSPACE_COOKIE = 'wotbot-workspace';
export const WORKSPACE_HEADER = 'X-WoTBot-Workspace';
export const DEFAULT_WORKSPACE_ID = 'default';

export function workspaceHeaderFor(
  workspaceId: string | undefined | null,
): Record<string, string> {
  const value = workspaceId?.trim();
  return value && value !== DEFAULT_WORKSPACE_ID
    ? { [WORKSPACE_HEADER]: value }
    : {};
}
