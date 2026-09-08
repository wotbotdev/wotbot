'use client';

import { memo, useState } from 'react';
import { Expand, X } from 'lucide-react';

import { ArtifactPreview } from '@/components/wotbot/chat-tool-calls/artifact-preview';
import { VisibilityToggle } from '@/components/wotbot/chat-tool-calls/tool-card-shell';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Collapsible, CollapsibleContent } from '@/components/ui/collapsible';
import {
  Drawer,
  DrawerClose,
  DrawerContent,
  DrawerDescription,
  DrawerHeader,
  DrawerTitle,
} from '@/components/ui/drawer';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';

import { type RunCodeArtifact } from '../chat-tool-call-model';
import { FileArtifactCard } from './file-artifact-card';

export const RunCodeArtifactCard = memo(function RunCodeArtifactCard({
  artifact,
}: {
  artifact: RunCodeArtifact;
}) {
  const [isFullscreenOpen, setIsFullscreenOpen] = useState(false);
  const [showPreview, setShowPreview] = useState(true);
  if (artifact.kind === 'file') return <FileArtifactCard artifact={artifact} />;
  const artifactType =
    artifact.kind === 'plotly' ? 'Interactive chart' : 'Generated image';

  return (
    <Drawer
      direction="right"
      handleOnly
      onOpenChange={setIsFullscreenOpen}
      open={isFullscreenOpen}
    >
      <Collapsible open={showPreview} onOpenChange={setShowPreview}>
        <Card className="gap-0 border border-border/55 bg-background/45 py-0 shadow-none ring-0">
          <CardContent className="space-y-2 py-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex min-w-0 items-center gap-2 px-0.5">
                <Badge
                  className="h-5 font-mono text-[0.66rem]"
                  variant="outline"
                >
                  {artifact.ref}
                </Badge>
                <span className="truncate text-[0.7rem] text-muted-foreground">
                  {artifactType}
                </span>
              </div>

              <div className="flex items-center gap-1">
                <VisibilityToggle expanded={showPreview} />

                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      aria-label="Open fullscreen preview"
                      className="text-muted-foreground hover:text-foreground"
                      onClick={() => setIsFullscreenOpen(true)}
                      size="icon-xs"
                      type="button"
                      variant="ghost"
                    >
                      <Expand className="size-3.5" />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent side="top">Open fullscreen</TooltipContent>
                </Tooltip>
              </div>
            </div>

            <CollapsibleContent className="data-closed:hidden">
              <ArtifactPreview artifact={artifact} />
            </CollapsibleContent>
          </CardContent>
        </Card>

        {isFullscreenOpen ? (
          <DrawerContent
            className="h-full gap-0 overflow-hidden p-0"
            style={{ width: 'min(100vw, 88rem)', maxWidth: 'none' }}
          >
            <DrawerHeader className="flex-row items-center justify-between gap-3 border-b border-border/55 px-4 py-2.5">
              <DrawerTitle className="flex min-w-0 items-center gap-2 text-sm">
                <Badge
                  className="h-5 font-mono text-[0.66rem]"
                  variant="outline"
                >
                  {artifact.ref}
                </Badge>
                <span className="truncate">{artifactType}</span>
              </DrawerTitle>
              <DrawerDescription className="sr-only">
                Full-size preview for {artifact.filename}
              </DrawerDescription>
              <DrawerClose asChild>
                <Button aria-label="Close" size="icon-sm" variant="ghost">
                  <X className="size-3.5" />
                </Button>
              </DrawerClose>
            </DrawerHeader>

            <div className="min-h-0 flex-1 overflow-auto p-2 sm:p-3">
              <ArtifactPreview artifact={artifact} fill />
            </div>
          </DrawerContent>
        ) : null}
      </Collapsible>
    </Drawer>
  );
});
