'use client';

import type { ToolCallMessagePartProps } from '@assistant-ui/react';

import { FileArtifactCard } from '@/components/wotbot/chat-tool-calls/file-artifact-card';
import { GenericToolCallCard } from '@/components/wotbot/chat-tool-calls/generic-tool-call-card';
import {
  RunCodeArtifacts,
  RunCodeCard,
} from '@/components/wotbot/chat-tool-calls/run-code-card';
import {
  WebInterfaceArtifactView,
  WebInterfaceCard,
} from '@/components/wotbot/chat-tool-calls/web-interface-card';
import {
  enrichArtifactForPinning,
  normalizeWebInterfaceResult,
} from '@/components/wotbot/chat-tool-calls/web-interface-model';
import {
  artifactKey,
  hasErrorResult,
  normalizeRunCodeResult,
  type CatchAllToolCallRenderProps,
  type ToolCallStatus,
} from '@/components/wotbot/chat-tool-call-model';
import { useReportToolCall } from '@/components/wotbot/assistant/thought-group';
import {
  ARTIFACT_VIEW_NAME,
  FILE_VIEW_NAME,
  WOT_SUMMARY_NAME,
} from '@/lib/thread-messages';
import { WotInteractionSummaryCard } from '@/components/wotbot/wot-summary/wot-interaction-summary-card';
import type { WotInteraction } from '@/lib/wot-interactions';

/**
 * Renders one tool call through the existing cards.
 *
 * Runs of tool calls are not coalesced here: `MessagePrimitive.GroupedParts`
 * groups adjacent parts structurally, so each call arrives as its own part and
 * this only has to render it. Where a call lands -- inside the collapsed
 * thought block or standing on its own -- is decided by `wotbotGroupBy`.
 */

function toCardStatus(
  status: ToolCallMessagePartProps['status'],
  hasResult: boolean,
): ToolCallStatus {
  if (hasResult) {
    return 'complete';
  }
  return status?.type === 'running' ? 'executing' : 'inProgress';
}

function toRenderProps(
  props: ToolCallMessagePartProps,
): CatchAllToolCallRenderProps {
  return {
    args: props.args,
    name: props.toolName,
    result: props.result,
    status: toCardStatus(props.status, props.result !== undefined),
  };
}

/**
 * A tool call inside the thought block: the compact card only.
 *
 * It also reports itself upward so the block can title itself and open on a
 * failure, which it cannot work out from its own children.
 */
export function GroupedToolCall(props: ToolCallMessagePartProps) {
  const rendered = toRenderProps(props);
  const isComplete = rendered.status === 'complete';
  const hasError =
    (props as { isError?: boolean }).isError === true ||
    (isComplete && hasErrorResult(rendered.result));

  useReportToolCall(props.toolCallId, hasError);

  if (props.toolName === 'run_code') {
    return <RunCodeCard {...rendered} showArtifacts={false} />;
  }
  if (props.toolName === 'create_web_interface') {
    return <WebInterfaceCard {...rendered} showInterface={false} />;
  }
  return <GenericToolCallCard {...rendered} />;
}

/**
 * A part that belongs outside the thought block, at full width.
 *
 * An artifact split off from the call that produced it, the files a call
 * offers for download, or the device-interaction summary, which is a statement
 * of what changed rather than working.
 */
export function StandaloneToolCall(props: ToolCallMessagePartProps) {
  if (props.toolName === ARTIFACT_VIEW_NAME) {
    const { source, sourceArgs } = (props.args ?? {}) as {
      source?: string;
      sourceArgs?: unknown;
    };

    if (source === 'run_code') {
      // Files are offered at the end of the turn instead; see FILE_VIEW_NAME.
      const result = normalizeRunCodeResult(props.result);
      const visuals = result.artifacts?.filter(
        (artifact) => artifact.kind !== 'file',
      );
      return visuals?.length ? (
        <div className="my-1">
          <RunCodeArtifacts result={{ ...result, artifacts: visuals }} />
        </div>
      ) : null;
    }

    if (source !== 'create_web_interface') {
      return null;
    }

    const parsed = normalizeWebInterfaceResult(props.result);
    return parsed.artifact ? (
      <div className="my-1">
        <WebInterfaceArtifactView
          artifact={enrichArtifactForPinning(parsed.artifact, sourceArgs)}
        />
      </div>
    ) : null;
  }

  if (props.toolName === FILE_VIEW_NAME) {
    const files = normalizeRunCodeResult(props.result).artifacts?.filter(
      (artifact) => artifact.kind === 'file',
    );
    return files?.map((artifact) => (
      <FileArtifactCard artifact={artifact} key={artifactKey(artifact)} />
    ));
  }

  if (props.toolName === WOT_SUMMARY_NAME) {
    const interactions =
      (props.args as { interactions?: WotInteraction[] })?.interactions ?? [];
    return <WotInteractionSummaryCard interactions={interactions} />;
  }

  return (
    <div className="my-1">
      <GenericToolCallCard {...toRenderProps(props)} />
    </div>
  );
}
