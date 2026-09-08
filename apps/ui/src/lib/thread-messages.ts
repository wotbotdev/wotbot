import type { ThreadMessageLike } from '@assistant-ui/react';

import {
  looksLikeDeviceInteractionSummaryContent,
  parseDeviceInteractionSummaryContent,
} from '@/lib/wot-interactions';

/**
 * Converts LangChain messages (as they arrive over the wire from LangGraph)
 * into assistant-ui's `ThreadMessageLike`.
 *
 * The one piece of real translation in the chat stack. Two things make it more
 * than a field rename:
 *
 * 1. LangChain models a tool result as its own `tool` message following the
 *    `ai` message that requested it. assistant-ui instead nests the result
 *    inside the assistant message's `tool-call` part, so results are folded
 *    backwards onto the call they answer rather than emitted as messages.
 * 2. `content` is either a plain string or an array of typed blocks, and
 *    reasoning blocks have to survive as reasoning rather than be flattened
 *    into the visible answer.
 * 3. Some providers report reasoning outside `content` entirely. OpenRouter
 *    returns it in its own field, which `ChatOpenRouter` keeps in
 *    `additional_kwargs.reasoning`; it becomes a reasoning part here so both
 *    shapes render through the same component.
 */

type Json = Record<string, unknown>;

export type LangChainMessage = {
  type?: string;
  content?: unknown;
  id?: string | null;
  tool_calls?: Array<{
    id?: string | null;
    name?: string;
    args?: unknown;
  }> | null;
  tool_call_id?: string | null;
  status?: string | null;
  additional_kwargs?: Json | null;
};

type ToolCallPart = {
  type: 'tool-call';
  toolCallId: string;
  toolName: string;
  args: Json;
  result?: unknown;
  isError?: boolean;
};

/**
 * Synthetic tool name for a device-interaction summary turn.
 *
 * The agent emits a machine-readable summary of the WoT calls it made as an
 * ordinary assistant message. It is rendered as a summary card rather than as
 * prose, so it is recognised here and re-tagged as its own part; a turn that
 * looks like one but does not parse is dropped rather than shown raw.
 */
export const WOT_SUMMARY_NAME = '__wotbot_wot_summary__';

/**
 * Tools whose result carries something to show: a plot, a generated interface.
 */
const ARTIFACT_TOOLS: ReadonlySet<string> = new Set([
  'run_code',
  'create_web_interface',
]);

/**
 * Synthetic part that renders an artifact-producing tool's output on its own.
 *
 * The tool's card belongs with the others inside the collapsed thought block,
 * but its artifact is the answer and must stay visible. A part renders where
 * the group tree puts it, so the artifact is split off into an ungrouped part
 * of its own, emitted once the turn's calls are known.
 */
export const ARTIFACT_VIEW_NAME = '__wotbot_artifact__';

export type GroupedToolCall = {
  id: string;
  name: string;
  args: Json;
  result?: unknown;
  isError?: boolean;
};

type TextPart = { type: 'text'; text: string };
type ReasoningPart = { type: 'reasoning'; text: string };
type ContentPart = TextPart | ReasoningPart | ToolCallPart;

function isRecord(value: unknown): value is Json {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Tool results cross the wire as strings. The cards downstream expect the
 * decoded object (artifacts, stdout, wotInteractions), so decode when it is
 * JSON and pass the raw string through when it is not.
 */
function decodeToolResult(content: unknown): unknown {
  if (typeof content !== 'string') {
    return content;
  }
  const trimmed = content.trim();
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) {
    return content;
  }
  try {
    return JSON.parse(trimmed);
  } catch {
    return content;
  }
}

/** Normalizes both string content and LangChain's typed content blocks. */
function toContentParts(content: unknown): ContentPart[] {
  if (typeof content === 'string') {
    return content ? [{ type: 'text', text: content }] : [];
  }
  if (!Array.isArray(content)) {
    return [];
  }

  const parts: ContentPart[] = [];
  for (const block of content) {
    if (typeof block === 'string') {
      if (block) parts.push({ type: 'text', text: block });
      continue;
    }
    if (!isRecord(block)) continue;

    const blockType = block.type;
    if (blockType === 'text' && typeof block.text === 'string') {
      if (block.text) parts.push({ type: 'text', text: block.text });
    } else if (
      (blockType === 'reasoning' || blockType === 'thinking') &&
      typeof (block.text ?? block.thinking) === 'string'
    ) {
      const text = (block.text ?? block.thinking) as string;
      if (text) parts.push({ type: 'reasoning', text });
    }
  }
  return parts;
}

/** Reasoning a provider reported beside `content` rather than inside it. */
function toDetachedReasoningParts(message: LangChainMessage): ContentPart[] {
  const reasoning = message.additional_kwargs?.reasoning;
  if (typeof reasoning !== 'string' || !reasoning.trim()) {
    return [];
  }
  return [{ type: 'reasoning', text: reasoning }];
}

function toToolCallParts(message: LangChainMessage): ToolCallPart[] {
  if (!Array.isArray(message.tool_calls)) {
    return [];
  }
  const calls: ToolCallPart[] = [];
  for (const call of message.tool_calls) {
    // A call with no id cannot be joined to the `tool` message carrying its
    // result, so a card for it would sit at "executing" forever. Synthesizing
    // an id only hides that: the result arrives keyed by the provider's own id
    // and never matches.
    if (!call?.id || !call.name) continue;
    calls.push({
      type: 'tool-call',
      toolCallId: call.id,
      toolName: call.name,
      args: isRecord(call.args) ? call.args : ({} as Json),
    });
  }
  return calls;
}

function convertThreadMessages(
  messages: readonly LangChainMessage[] | undefined,
  readToolResult = (message: LangChainMessage) =>
    decodeToolResult(message.content),
  readSummary = (message: LangChainMessage) =>
    parseDeviceInteractionSummaryContent(message.content),
): ThreadMessageLike[] {
  if (!messages?.length) {
    return [];
  }

  const result: ThreadMessageLike[] = [];
  // Lets a `tool` message find the call it answers, however many assistant
  // messages back that was.
  const callsById = new Map<string, ToolCallPart>();
  // The assistant turn being assembled. A turn arrives as several LangChain
  // messages -- one per agent step -- but becomes a single message here, so
  // reasoning, tool calls and the final answer keep their true order and
  // `MessagePrimitive.GroupedParts` can coalesce adjacent runs of them.
  let turn: { id?: string; parts: ContentPart[] } | null = null;

  const closeTurn = () => {
    if (turn) {
      // Each artifact goes at the end of the run of calls it belongs to, not
      // at the end of the turn: a turn that produces two artifacts with prose
      // between them would otherwise stack both after the second one, putting
      // the first below the sentence that says it is above.
      const placed: ContentPart[] = [];
      let pending: ContentPart[] = [];

      for (const part of turn.parts) {
        const isCall =
          part.type === 'tool-call' && part.toolName !== WOT_SUMMARY_NAME;
        // Only a part that ends the run flushes. Reasoning stays inside the
        // thought block, so flushing there would wedge the artifact between two
        // halves of what should read as one block.
        const endsRun =
          part.type === 'text' ||
          (part.type === 'tool-call' && part.toolName === WOT_SUMMARY_NAME);
        if (endsRun && pending.length) {
          placed.push(...pending);
          pending = [];
        }
        placed.push(part);
        if (
          isCall &&
          ARTIFACT_TOOLS.has(part.toolName) &&
          part.result !== undefined
        ) {
          pending.push({
            type: 'tool-call',
            toolCallId: `artifact:${part.toolCallId}`,
            toolName: ARTIFACT_VIEW_NAME,
            args: { source: part.toolName, sourceArgs: part.args },
            result: part.result,
          });
        }
      }
      placed.push(...pending);
      turn.parts = placed;
    }
    // A turn with no parts would render as an empty bubble.
    if (turn && turn.parts.length) {
      result.push({
        role: 'assistant',
        content: turn.parts as ThreadMessageLike['content'],
        ...(turn.id ? { id: turn.id } : {}),
      });
    }
    turn = null;
  };

  const turnParts = (id?: string | null): ContentPart[] => {
    // The first step's id names the turn and stays put, so the message keeps
    // its identity while later steps stream in and append to it.
    turn ??= { parts: [], ...(id ? { id } : {}) };
    return turn.parts;
  };

  for (const message of messages) {
    const type = message.type;

    if (type === 'tool') {
      const call = message.tool_call_id
        ? callsById.get(message.tool_call_id)
        : undefined;
      if (call) {
        call.result = readToolResult(message);
        if (message.status === 'error') {
          call.isError = true;
        }
      }
      // A tool result is not a part of its own: it lands on the call it answers.
      continue;
    }

    if (type === 'human' || type === 'user') {
      closeTurn();
      const content = toContentParts(message.content);
      if (content.length) {
        result.push({
          role: 'user',
          content: content as ThreadMessageLike['content'],
          ...(message.id ? { id: message.id } : {}),
        });
      }
      continue;
    }

    if (type === 'ai' || type === 'assistant' || type === 'AIMessageChunk') {
      const interactions = readSummary(message);
      if (interactions.length > 0) {
        turnParts(message.id).push({
          type: 'tool-call',
          toolCallId: `wot:${message.id ?? result.length}`,
          toolName: WOT_SUMMARY_NAME,
          args: { interactions },
        });
        continue;
      }
      if (looksLikeDeviceInteractionSummaryContent(message.content)) {
        // Unparseable summary: hide it rather than render the raw payload.
        continue;
      }

      const parts = turnParts(message.id);
      parts.push(...toDetachedReasoningParts(message));
      parts.push(...toContentParts(message.content));
      for (const call of toToolCallParts(message)) {
        callsById.set(call.toolCallId, call);
        parts.push(call);
      }
      continue;
    }

    if (type === 'system') {
      // Job transcripts carry run-lifecycle lines as system messages; the chat
      // checkpoint never contains any, so emitting them here is safe.
      closeTurn();
      const content = toContentParts(message.content);
      if (content.length) {
        result.push({
          role: 'system',
          content: content as ThreadMessageLike['content'],
          ...(message.id ? { id: message.id } : {}),
        });
      }
      continue;
    }
  }

  closeTurn();
  return result;
}

export function toThreadMessages(
  messages: readonly LangChainMessage[] | undefined,
): ThreadMessageLike[] {
  return convertThreadMessages(messages);
}

/** Stable callback: changing this identity clears assistant-ui's converter cache. */
export const identityThreadMessage = (message: ThreadMessageLike) => message;

function shallowEqual(a: Json, b: Json): boolean {
  const keys = Object.keys(a);
  return (
    keys.length === Object.keys(b).length &&
    keys.every((key) => Object.hasOwn(b, key) && a[key] === b[key])
  );
}

function samePart(a: ContentPart, b: ContentPart): boolean {
  if (a.type !== b.type) return false;
  if (a.type !== 'tool-call' || b.type !== 'tool-call') {
    return shallowEqual(a, b);
  }
  return (
    a.toolCallId === b.toolCallId &&
    a.toolName === b.toolName &&
    a.result === b.result &&
    a.isError === b.isError &&
    shallowEqual(a.args, b.args)
  );
}

/**
 * Per-runtime conversion for immutable LangGraph snapshots. Decode each tool
 * result/summary once, then preserve equal messages and parts across tokens so
 * completed cards and Markdown can keep their render caches. Weak keys let old
 * stream snapshots and edited-away results be collected.
 */
export function createThreadMessageConverter() {
  const results = new WeakMap<LangChainMessage, unknown>();
  const summaries = new WeakMap<
    LangChainMessage,
    ReturnType<typeof parseDeviceInteractionSummaryContent>
  >();
  let previous: ThreadMessageLike[] = [];

  return (
    messages: readonly LangChainMessage[] | undefined,
  ): ThreadMessageLike[] => {
    const next = convertThreadMessages(
      messages,
      (message) => {
        if (!results.has(message))
          results.set(message, decodeToolResult(message.content));
        return results.get(message);
      },
      (message) => {
        let summary = summaries.get(message);
        if (!summary) {
          summary = parseDeviceInteractionSummaryContent(message.content);
          summaries.set(message, summary);
        }
        return summary;
      },
    ).map((message, index) => {
      const old = previous[index];
      if (!old || old.id !== message.id || old.role !== message.role)
        return message;
      const oldParts = old.content as ContentPart[];
      const newParts = message.content as ContentPart[];
      const content = newParts.map((part, partIndex) =>
        oldParts[partIndex] && samePart(oldParts[partIndex], part)
          ? oldParts[partIndex]
          : part,
      );
      return content.length === oldParts.length &&
        content.every((part, i) => part === oldParts[i])
        ? old
        : { ...message, content: content as ThreadMessageLike['content'] };
    });
    if (
      next.length === previous.length &&
      next.every((message, i) => message === previous[i])
    ) {
      return previous;
    }
    previous = next;
    return next;
  };
}
