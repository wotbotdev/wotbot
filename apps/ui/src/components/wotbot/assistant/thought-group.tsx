'use client';

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { ChevronDown, CircleAlert } from 'lucide-react';

import { Button } from '@/components/ui/button';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import { cn } from '@/lib/utils';

/**
 * The single collapsed block holding a turn's reasoning and tool calls.
 *
 * Membership is decided structurally by `wotbotGroupBy`; this component only
 * renders the shell. It cannot inspect its own children, so the two things it
 * needs to know about them -- how many tool calls there are, and whether any
 * failed -- are reported upwards through context by the parts themselves.
 */

type ThoughtGroupReport = {
  setTool: (toolCallId: string, present: boolean) => void;
  setError: (toolCallId: string, failed: boolean) => void;
};

const ThoughtGroupContext = createContext<ThoughtGroupReport | null>(null);

function useThoughtGroupReport(): ThoughtGroupReport | null {
  return useContext(ThoughtGroupContext);
}

/**
 * Registers a tool call with the enclosing block for as long as it is shown.
 *
 * Deregistering on unmount matters: messages render by index, so regenerating a
 * response or switching branches reuses this block instance. Without the
 * cleanup it would keep the previous run's count and failure marker for parts
 * that are no longer on screen.
 */
export function useReportToolCall(toolCallId: string, hasError: boolean) {
  const report = useThoughtGroupReport();

  useEffect(() => {
    report?.setTool(toolCallId, true);
    return () => report?.setTool(toolCallId, false);
  }, [report, toolCallId]);

  useEffect(() => {
    report?.setError(toolCallId, hasError);
    return () => report?.setError(toolCallId, false);
  }, [hasError, report, toolCallId]);
}

/** Mirrors the device-interaction card: a fixed title over a counts line. */
function formatDetail(toolCount: number, errorCount: number): string {
  const parts = [`${toolCount} tool${toolCount === 1 ? '' : 's'}`];
  if (errorCount) {
    parts.push(`${errorCount} failed`);
  }
  return parts.join(' · ');
}

/** Add or remove an id without churning the set when nothing changed. */
function toggleId(
  current: ReadonlySet<string>,
  id: string,
  present: boolean,
): ReadonlySet<string> {
  if (present === current.has(id)) {
    return current;
  }
  const next = new Set(current);
  if (present) {
    next.add(id);
  } else {
    next.delete(id);
  }
  return next;
}

export function ThoughtGroup({ children }: { children: ReactNode }) {
  const [isExpanded, setIsExpanded] = useState(false);
  const [errorIds, setErrorIds] = useState<ReadonlySet<string>>(new Set());
  const [toolIds, setToolIds] = useState<ReadonlySet<string>>(new Set());

  const report = useMemo<ThoughtGroupReport>(
    () => ({
      setTool: (toolCallId, present) =>
        setToolIds((current) => toggleId(current, toolCallId, present)),
      setError: (toolCallId, failed) =>
        setErrorIds((current) => toggleId(current, toolCallId, failed)),
    }),
    [],
  );
  const hasError = errorIds.size > 0;

  // Failures stay collapsed: the header's icon and "N failed" count flag them,
  // and the agent usually recovers on its own, so opening would only push the
  // answer out of view.
  const detail = formatDetail(toolIds.size, errorIds.size);

  return (
    <ThoughtGroupContext.Provider value={report}>
      <Collapsible
        className="wotbot-tool-call my-1 space-y-2"
        open={isExpanded}
        onOpenChange={setIsExpanded}
      >
        {/* Same header shape as the tool and device-interaction cards, so the
            chat reads as one family of blocks rather than three. */}
        <div className="flex flex-wrap items-center justify-between gap-2 px-1 py-1">
          <div className="flex min-w-0 items-center gap-2">
            {/* No resting icon: the block leads with text like the device-
                interaction card. Only a failure earns one -- the turn's single
                activity animation is the standalone indicator that
                `GroupedParts` emits below, so the block never animates too. */}
            {hasError ? (
              <CircleAlert className="size-3.5 shrink-0 text-destructive" />
            ) : null}
            <div className="min-w-0 space-y-0.5">
              <p className="truncate text-[0.76rem] font-medium text-foreground">
                Thought process
              </p>
              {toolIds.size > 0 ? (
                <div className="truncate text-[0.7rem] text-muted-foreground">
                  {detail}
                </div>
              ) : null}
            </div>
          </div>

          <CollapsibleTrigger asChild>
            <Button
              className="text-[0.66rem] font-medium text-muted-foreground hover:text-foreground"
              size="xs"
              type="button"
              variant="ghost"
            >
              <span>{isExpanded ? 'Hide details' : 'Details'}</span>
              <ChevronDown
                className={cn(
                  'size-3 transition-transform',
                  isExpanded && 'rotate-180',
                )}
              />
            </Button>
          </CollapsibleTrigger>
        </div>

        {/* forceMount: the parts inside report themselves upward, and a
            collapsed block that unmounted them would title itself "Thought
            process" until first opened, then change once it could count. */}
        <CollapsibleContent forceMount className="data-[state=closed]:hidden">
          <div className="space-y-1 border-l border-border/60 pl-3">
            {children}
          </div>
        </CollapsibleContent>
      </Collapsible>
    </ThoughtGroupContext.Provider>
  );
}
