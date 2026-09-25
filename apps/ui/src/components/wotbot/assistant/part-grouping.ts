import {
  ARTIFACT_VIEW_NAME,
  FILE_VIEW_NAME,
  WOT_SUMMARY_NAME,
} from '@/lib/thread-messages';

/**
 * How a turn's parts coalesce into blocks.
 *
 * `MessagePrimitive.GroupedParts` walks the parts in order and keeps a group
 * open while consecutive parts share its path prefix. Reasoning and tool calls
 * therefore fold into one `group-chainOfThought` block spanning the whole working
 * phase, with the answer -- whose path is empty -- closing it. This replaces
 * the coalescing that `toThreadMessages` used to do by hand, which could only
 * group within a message and so lost the true interleaving of a tool loop.
 */
export const GROUP_THOUGHT = 'group-chainOfThought' as const;
export const GROUP_REASONING = 'group-reasoning' as const;
export const GROUP_TOOL = 'group-tool' as const;
/** The turn's downloads: one part per call, framed as a single block. */
export const GROUP_DOWNLOADS = 'group-downloads' as const;

/** The key shape `GroupedParts` requires, so a typo cannot silently ungroup. */
type GroupKey = `group-${string}`;

// Frozen module-level paths: `GroupedParts` memoizes its tree on the identity
// of the groupBy function, so nothing here may be rebuilt per render.
const REASONING_PATH: readonly GroupKey[] = Object.freeze([
  GROUP_THOUGHT,
  GROUP_REASONING,
]);
const TOOL_PATH: readonly GroupKey[] = Object.freeze([
  GROUP_THOUGHT,
  GROUP_TOOL,
]);
const DOWNLOADS_PATH: readonly GroupKey[] = Object.freeze([GROUP_DOWNLOADS]);
const UNGROUPED: readonly GroupKey[] = Object.freeze([]);

type GroupablePart = { type: string; toolName?: string };

/**
 * Hand-written rather than built with the documented `groupPartByType` helper:
 * that helper's `standalone-tool-call` opt-out is resolved from the tool-UI
 * registry (`toolUIs[name].standalone`), which this app does not use -- its
 * tools render through cards keyed by name. The group keys match the ones in
 * assistant-ui's docs so the two read the same. Module-level so `GroupedParts`
 * can memoize its tree on the function's identity.
 */
export function wotbotGroupBy(part: GroupablePart): readonly GroupKey[] {
  if (part.type === 'reasoning') {
    return REASONING_PATH;
  }
  if (part.type === 'tool-call') {
    if (part.toolName === FILE_VIEW_NAME) return DOWNLOADS_PATH;
    return part.toolName && isStandalonePart(part.toolName)
      ? UNGROUPED
      : TOOL_PATH;
  }
  return UNGROUPED;
}

/**
 * Whether a part is output rather than working, so it renders outside the block.
 *
 * Every real tool call groups. The only exceptions are the parts
 * `toThreadMessages` synthesizes for display: an artifact split off from the
 * call that produced it -- a plot, a generated interface, the thing the user
 * actually asked for -- the files offered for download, and the
 * device-interaction summary.
 */
export function isStandalonePart(toolName: string): boolean {
  return (
    toolName === ARTIFACT_VIEW_NAME ||
    toolName === FILE_VIEW_NAME ||
    toolName === WOT_SUMMARY_NAME
  );
}
