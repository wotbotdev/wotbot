'use client';

import { Download, File } from 'lucide-react';

import { Button } from '@/components/ui/button';
import type { RunCodeArtifact } from '../chat-tool-call-model';

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
  const expires =
    artifact.expires_at && Number.isFinite(Date.parse(artifact.expires_at))
      ? new Date(artifact.expires_at).toUTCString()
      : null;

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
            Available until {expires}
          </p>
        ) : null}
      </div>
      {artifact.id ? (
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
