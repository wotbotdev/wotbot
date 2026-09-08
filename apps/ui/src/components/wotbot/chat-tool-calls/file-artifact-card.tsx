'use client';

import { Download, File } from 'lucide-react';
import { useCallback, useSyncExternalStore } from 'react';

import { Button } from '@/components/ui/button';
import type { RunCodeArtifact } from '../chat-tool-call-model';

/**
 * Whether the executor has swept the file away, kept current while it is shown.
 *
 * The clock is an external store rather than render state: a timer fires at the
 * moment the file lapses, so a card left open stops offering a download that
 * would only 404.
 */
function useHasExpired(expiresAt: number): boolean {
  const subscribe = useCallback(
    (onExpiry: () => void) => {
      const remaining = expiresAt - Date.now();
      // Nothing to wait for when it has already lapsed, and setTimeout
      // truncates past its 32-bit range -- those expiries are days out and get
      // picked up the next time the thread is opened.
      if (
        !Number.isFinite(remaining) ||
        remaining <= 0 ||
        remaining > 2 ** 31 - 1
      ) {
        return () => {};
      }
      const timer = window.setTimeout(onExpiry, remaining);
      return () => window.clearTimeout(timer);
    },
    [expiresAt],
  );

  return useSyncExternalStore(
    subscribe,
    () => Number.isFinite(expiresAt) && expiresAt <= Date.now(),
    () => false,
  );
}

export function FileArtifactCard({ artifact }: { artifact: RunCodeArtifact }) {
  const size = artifact.size_bytes;
  const sizeLabel =
    size === undefined
      ? ''
      : size < 1024
        ? `${size} B`
        : size < 1024 * 1024
          ? `${(size / 1024).toFixed(1)} KB`
          : `${(size / (1024 * 1024)).toFixed(1)} MB`;
  const expiresAt = artifact.expires_at
    ? Date.parse(artifact.expires_at)
    : Number.NaN;
  const expires = Number.isFinite(expiresAt)
    ? new Date(expiresAt).toUTCString()
    : null;
  const expired = useHasExpired(expiresAt);

  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border/55 bg-background/45 p-3">
      <File className="size-5 shrink-0 text-muted-foreground" aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="break-all text-sm font-medium">{artifact.filename}</p>
        <p className="text-xs text-muted-foreground">
          {[sizeLabel, artifact.mime_type].filter(Boolean).join(' · ')}
        </p>
        {expires ? (
          <p className="text-xs text-muted-foreground">
            {expired ? 'Expired on' : 'Available until'} {expires}
          </p>
        ) : null}
      </div>
      {expired ? (
        <Button disabled size="sm" variant="outline">
          Expired
        </Button>
      ) : artifact.id ? (
        <Button asChild size="sm" variant="outline">
          <a
            href={`/api/artifacts/${encodeURIComponent(artifact.id)}`}
            download={artifact.filename}
          >
            <Download className="size-3.5" aria-hidden />
            Download
          </a>
        </Button>
      ) : null}
    </div>
  );
}
