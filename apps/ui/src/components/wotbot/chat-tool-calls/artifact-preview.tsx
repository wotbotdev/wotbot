'use client';

import { memo } from 'react';

import { cn } from '@/lib/utils';
import { getPanelOrigin } from '@/lib/panel-origin';

import { type RunCodeArtifact } from '../chat-tool-call-model';
import { FileArtifactCard } from './file-artifact-card';

const PlotlyChart = memo(function PlotlyChart({
  className,
  filename,
  title,
}: {
  className?: string;
  filename: string;
  title: string;
}) {
  return (
    <iframe
      className={cn(
        'w-full rounded-lg border border-border/55 bg-background',
        className ?? 'h-[24rem]',
      )}
      loading="lazy"
      sandbox="allow-scripts allow-same-origin"
      src={`${getPanelOrigin(filename)}/api/artifacts/${encodeURIComponent(filename)}`}
      title={title}
    />
  );
});

export function ArtifactPreview({
  artifact,
  fullscreen = false,
  fill = false,
}: {
  artifact: RunCodeArtifact;
  fullscreen?: boolean;
  fill?: boolean;
}) {
  if (artifact.kind === 'file') {
    return <FileArtifactCard artifact={artifact} />;
  }
  if (artifact.kind === 'image') {
    return (
      // eslint-disable-next-line @next/next/no-img-element -- generated code artifacts are proxied files and do not benefit from Next image optimization
      <img
        alt={artifact.ref}
        className={cn(
          'rounded-lg border border-border/55',
          fullscreen
            ? 'mx-auto max-h-[78vh] w-auto rounded-xl object-contain'
            : fill
              ? 'mx-auto max-h-full max-w-full object-contain'
              : 'w-full max-w-full',
        )}
        // Plain image, on the app's origin: it executes nothing, so a separate
        // origin isolates nothing while costing a DNS lookup and TLS handshake
        // per image -- and breaking wherever wildcard resolution is absent.
        src={`/api/artifacts/${encodeURIComponent(artifact.filename)}`}
      />
    );
  }

  return (
    <PlotlyChart
      className={
        fullscreen
          ? 'h-[78vh] rounded-xl'
          : fill
            ? 'h-full w-full'
            : 'h-[24rem]'
      }
      filename={artifact.filename}
      title={`Chart ${artifact.ref}`}
    />
  );
}
