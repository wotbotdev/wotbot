'use client';

import { memo, useMemo, useState } from 'react';
import { CircleAlert } from 'lucide-react';

import { RunCodeArtifactCard } from '@/components/wotbot/chat-tool-calls/run-code-artifact-card';
import {
  DetailsToggle,
  ToolCardHeader,
} from '@/components/wotbot/chat-tool-calls/tool-card-shell';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Collapsible, CollapsibleContent } from '@/components/ui/collapsible';

import {
  artifactKey,
  formatArtifactSummary,
  formatToolName,
  formatWotInteractionSummary,
  normalizeRunCodeResult,
  type CatchAllToolCallRenderProps,
  type RunCodeResult,
} from '../chat-tool-call-model';

export function RunCodeArtifacts({ result }: { result: RunCodeResult }) {
  if (!(result.artifacts?.length ?? 0)) {
    return null;
  }

  return (
    <div className="space-y-2">
      {result.artifacts?.map((artifact) => (
        <RunCodeArtifactCard key={artifactKey(artifact)} artifact={artifact} />
      ))}
    </div>
  );
}

function RunCodeDetails({ result }: { result: RunCodeResult }) {
  const hasStdout = !!result.stdout?.trim();
  const hasError = !!result.error;

  if (!hasStdout && !hasError) {
    return null;
  }

  return (
    <div className="space-y-2 rounded-lg border border-border/55 bg-background/40 p-2.5">
      {hasError ? (
        <Alert
          className="border-destructive/25 bg-destructive/5"
          variant="destructive"
        >
          <CircleAlert className="h-4 w-4" />
          <AlertTitle>Execution failed</AlertTitle>
          <AlertDescription>
            <pre className="overflow-auto font-mono text-[0.72rem] leading-5 whitespace-pre-wrap text-destructive">
              {result.error}
            </pre>
          </AlertDescription>
        </Alert>
      ) : null}

      {hasStdout ? (
        <pre className="max-h-52 overflow-auto rounded-lg border border-border/55 bg-background/80 px-3 py-2.5 text-[0.72rem] leading-5 whitespace-pre-wrap text-foreground">
          {result.stdout}
        </pre>
      ) : null}
    </div>
  );
}

export const RunCodeCard = memo(function RunCodeCard({
  args,
  result,
  showArtifacts = true,
  status,
}: CatchAllToolCallRenderProps & { showArtifacts?: boolean }) {
  const [showDetails, setShowDetails] = useState(false);
  const code = (args as { code?: string } | undefined)?.code ?? '';
  const parsedResult = useMemo(
    () => (status === 'complete' ? normalizeRunCodeResult(result) : {}),
    [status, result],
  );
  const wotInteractions = parsedResult.wotInteractions ?? [];
  const hasArtifacts = (parsedResult.artifacts?.length ?? 0) > 0;
  const hasStdout = !!parsedResult.stdout?.trim();
  const hasError = !!parsedResult.error;
  const hasWotInteractions = wotInteractions.length > 0;
  const artifactSummary = parsedResult.artifacts?.length
    ? formatArtifactSummary(parsedResult.artifacts)
    : '';
  const wotInteractionSummary = hasWotInteractions
    ? formatWotInteractionSummary(wotInteractions)
    : '';
  const completedSummary = [wotInteractionSummary, artifactSummary]
    .filter(Boolean)
    .join(' • ');
  const summary =
    status === 'executing'
      ? 'Executing analysis'
      : hasError
        ? 'Execution failed'
        : completedSummary ||
          (hasStdout ? 'Generated text output' : 'No visible output');
  const isCompleted = status === 'complete';
  const canShowDetails = code || hasStdout || hasError;

  return (
    <Collapsible
      className="wotbot-tool-call space-y-2"
      open={showDetails}
      onOpenChange={setShowDetails}
    >
      <ToolCardHeader
        action={
          canShowDetails ? <DetailsToggle expanded={showDetails} /> : undefined
        }
        hasError={hasError}
        isCompleted={isCompleted}
        status={status}
        summary={summary}
        title={formatToolName('run_code')}
      />

      <CollapsibleContent className="data-closed:hidden">
        <div className="space-y-2 rounded-lg border border-border/45 bg-background/35 p-2.5">
          {code ? (
            <pre className="max-h-52 overflow-auto rounded-lg border border-border/55 bg-muted/20 px-3 py-2.5 text-[0.72rem] leading-5 whitespace-pre-wrap text-foreground">
              {code}
            </pre>
          ) : null}
          <RunCodeDetails result={parsedResult} />
        </div>
      </CollapsibleContent>

      {status === 'executing' ? (
        <div className="px-0.5 text-[0.72rem] text-muted-foreground">
          Executing code…
        </div>
      ) : null}

      {showArtifacts && status === 'complete' && hasArtifacts ? (
        <RunCodeArtifacts result={parsedResult} />
      ) : null}

      {status === 'complete' &&
      !hasArtifacts &&
      !hasStdout &&
      !hasError &&
      !hasWotInteractions ? (
        <p className="px-0.5 text-[0.72rem] text-muted-foreground">
          No visible output.
        </p>
      ) : null}
    </Collapsible>
  );
});
