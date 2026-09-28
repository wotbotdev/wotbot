'use client';

import { useState, type ReactNode } from 'react';
import { useAuiState } from '@assistant-ui/react';
import { ChevronDown } from 'lucide-react';

import { Button } from '@/components/ui/button';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import { downloadFileCount } from '@/components/wotbot/assistant/downloads';
import { cn } from '@/lib/utils';

/**
 * The turn's downloads, collapsed like the device-interaction card so a few
 * saved files do not push the answer out of view.
 */
export function DownloadsGroup({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const count = useAuiState(downloadFileCount);

  return (
    <Collapsible
      aria-label="Downloads"
      className="wotbot-tool-call space-y-2"
      open={open}
      onOpenChange={setOpen}
    >
      <div className="flex flex-wrap items-center justify-between gap-2 px-1 py-1">
        <div className="min-w-0 space-y-0.5">
          <p className="truncate text-[0.76rem] font-medium text-foreground">
            Downloads
          </p>
          <div className="truncate text-[0.7rem] text-muted-foreground">
            {`${count} file${count === 1 ? '' : 's'}`}
          </div>
        </div>

        <CollapsibleTrigger asChild>
          <Button
            className="text-[0.66rem] font-medium text-muted-foreground hover:text-foreground"
            size="xs"
            type="button"
            variant="ghost"
          >
            <span>{open ? 'Hide details' : 'Details'}</span>
            <ChevronDown
              className={cn(
                'size-3 transition-transform',
                open && 'rotate-180',
              )}
            />
          </Button>
        </CollapsibleTrigger>
      </div>

      <CollapsibleContent className="data-closed:hidden">
        <div className="space-y-1.5 rounded-lg border border-border/45 bg-background/35 p-2.5">
          {children}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
