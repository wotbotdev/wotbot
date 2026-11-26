'use client';

import { useEffect, useState } from 'react';
import { toast } from 'sonner';

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

interface WorkspaceList {
  enabled: boolean;
  current: string;
  items: { id: string; title: string }[];
}

/**
 * Picks the workspace (use case) the UI works in. Catalog pages, chats and
 * the agent then only see that workspace. Hidden when the backend has no
 * workspaces or only the default one.
 */
export function WorkspaceSwitcher() {
  const [list, setList] = useState<WorkspaceList | null>(null);
  const [switching, setSwitching] = useState(false);

  useEffect(() => {
    fetch('/api/workspaces')
      .then((res) => (res.ok ? res.json() : null))
      .then((body: WorkspaceList | null) => setList(body))
      .catch(() => setList(null));
  }, []);

  if (!list?.enabled || list.items.length < 2) {
    return null;
  }

  async function select(id: string) {
    setSwitching(true);
    const res = await fetch('/api/workspaces', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id }),
    });
    if (!res.ok) {
      setSwitching(false);
      toast.error('Could not switch the workspace.');
      return;
    }
    // Start fresh: chats, Things and caches all belong to the old workspace.
    window.location.assign('/');
  }

  return (
    <div className="px-2 pb-1 group-data-[collapsible=icon]:hidden">
      <span className="mb-1 block text-xs text-muted-foreground">Use case</span>
      <Select value={list.current} onValueChange={select} disabled={switching}>
        <SelectTrigger size="sm" className="w-full" aria-label="Workspace">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {list.items.map((workspace) => (
            <SelectItem key={workspace.id} value={workspace.id}>
              {workspace.title}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
