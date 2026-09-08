'use client';

import { memo, useRef } from 'react';

import { useWotBridge } from '@/components/wotbot/chat-tool-calls/use-wot-bridge';
import { type WotCapability } from '@/components/wotbot/chat-tool-calls/web-interface-model';
import { cn } from '@/lib/utils';

/**
 * Sandboxed iframe for a generated WoT panel, wired to the capability-enforcing
 * postMessage bridge. Shared by ephemeral chat panels (src = artifact) and
 * pinned panels (src = panel render route).
 */
export const PanelFrame = memo(function PanelFrame({
  src,
  capabilities,
  title,
  className,
  interactive = true,
}: {
  src: string;
  capabilities: WotCapability[];
  title: string;
  className?: string;
  interactive?: boolean;
}) {
  const iframeRef = useRef<HTMLIFrameElement | null>(null);

  // `allow-same-origin` is only safe while `src` is genuinely another origin.
  // `getPanelOrigin` yields '' before hydration, and a misconfigured host
  // template could collapse to the app's own origin -- either would grant
  // untrusted panel code this origin's cookies, storage and API. Refuse rather
  // than silently downgrade.
  const isCrossOrigin =
    typeof window !== 'undefined' &&
    /^https?:\/\//.test(src) &&
    !src.startsWith(`${window.location.origin}/`);
  const canRunScripts = interactive && isCrossOrigin;

  useWotBridge(iframeRef, capabilities, { enabled: canRunScripts });

  return (
    <iframe
      ref={iframeRef}
      className={cn(
        'w-full rounded-lg border border-border/55 bg-background',
        className ?? 'h-[26rem]',
      )}
      // Untrusted, LLM-authored content. `allow-same-origin` is safe here only
      // because `src` points at the panel's own origin (see `panel-origin.ts`):
      // it means "your own origin", which is cross-origin to the app. Without
      // it the frame would be opaque-origin and denied every permission-gated
      // API. Preview frames keep scripts disabled entirely.
      // `xr-spatial-tracking` is what WebXR checks: without it delegated to the
      // frame, `navigator.xr` reports every session mode unsupported and every
      // call logs a permissions-policy violation. Delegating it is not a grant
      // -- entering an immersive session still needs a user gesture and, on
      // most devices, the browser's own consent prompt.
      allow={canRunScripts ? 'camera; microphone; xr-spatial-tracking' : ''}
      sandbox={canRunScripts ? 'allow-scripts allow-same-origin' : ''}
      src={src}
      title={title}
    />
  );
});
