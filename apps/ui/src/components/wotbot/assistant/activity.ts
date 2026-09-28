import { formatToolName } from '@/components/wotbot/chat-tool-call-model';
import { isStandalonePart } from '@/components/wotbot/assistant/part-grouping';

type ActivityPart = { type: string; toolName?: string; result?: unknown };

export type ActivityState = {
  message: { content: readonly ActivityPart[] };
};

/**
 * The tool the turn is waiting on, for the activity indicator.
 *
 * With the thought block collapsed, the indicator is the only live signal, so
 * naming the pending call tells the viewer what a long wait is spent on. The
 * latest call without a result wins; synthesized display parts never count.
 * Returns a string because it runs as a `useAuiState` selector, which must
 * return a primitive.
 */
export function pendingToolName(state: ActivityState): string {
  const parts = state.message.content;
  for (let i = parts.length - 1; i >= 0; i -= 1) {
    const part = parts[i];
    if (
      part.type === 'tool-call' &&
      part.toolName &&
      part.result === undefined &&
      !isStandalonePart(part.toolName)
    ) {
      return formatToolName(part.toolName);
    }
  }
  return '';
}
