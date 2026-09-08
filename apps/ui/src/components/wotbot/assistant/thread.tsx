'use client';

import {
  ActionBarPrimitive,
  AuiIf,
  BranchPickerPrimitive,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useAuiState,
} from '@assistant-ui/react';
import { MarkdownTextPrimitive } from '@assistant-ui/react-markdown';
import {
  ArrowDown,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  Copy,
  LoaderCircle,
  Pencil,
  RefreshCw,
  Square,
} from 'lucide-react';
import { motion, useReducedMotion } from 'motion/react';
import type { ComponentPropsWithoutRef, ReactNode } from 'react';
import type { ExtraProps } from 'react-markdown';

import {
  hasAssistantReloadAction,
  hasAssistantResponseActions,
} from '@/components/wotbot/assistant/message-actions';
import { messageAnchor } from '@/components/wotbot/assistant/artifacts';
import { ConversationFiles } from '@/components/wotbot/assistant/conversation-files';
import { markdownRemarkPlugins } from '@/components/wotbot/assistant/markdown';
import { ReasoningPart } from '@/components/wotbot/assistant/reasoning-ui';
import {
  GROUP_REASONING,
  GROUP_THOUGHT,
  GROUP_TOOL,
  isStandalonePart,
  wotbotGroupBy,
} from '@/components/wotbot/assistant/part-grouping';
import { ThoughtGroup } from '@/components/wotbot/assistant/thought-group';
import {
  GroupedToolCall,
  StandaloneToolCall,
} from '@/components/wotbot/assistant/tool-ui';
import { ThinkingIndicator } from '@/components/elements/thinking-indicator';
import { ConfirmDialog } from '@/components/confirm-dialog';
import { ErrorBoundary } from '@/components/error-boundary';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

/**
 * The chat shell, composed from assistant-ui primitives.
 *
 * The primitives supply what would otherwise be hand-written and fiddly --
 * autoscroll that yields to the user, streaming part updates, edit/branch
 * bookkeeping, keyboard handling, accessibility -- while the markup and
 * styling stay ours, so this matches the existing design rather than importing
 * someone else's.
 */

/** Whether the composer holds a draft, deciding Send vs. the live-mode button. */
const hasDraft = (state: { thread: { composer: { text: string } } }) =>
  Boolean(state.thread.composer.text.trim());

function MarkdownTable({
  node,
  ...tableProps
}: ComponentPropsWithoutRef<'table'> & ExtraProps) {
  void node;
  return (
    <div
      className={cn(
        'not-prose my-4 overflow-x-auto rounded-md border border-border',
        '[&_table]:w-full [&_table]:min-w-[36rem] [&_table]:border-collapse [&_table]:text-left [&_table]:text-sm',
        '[&_th]:border-b [&_th]:bg-muted/60 [&_th]:px-3 [&_th]:py-2 [&_th]:font-semibold',
        '[&_td]:border-b [&_td]:px-3 [&_td]:py-2 [&_td]:align-top',
        '[&_tbody_tr:last-child_td]:border-b-0',
      )}
    >
      <table {...tableProps} />
    </div>
  );
}

function MarkdownText() {
  return (
    <MarkdownTextPrimitive
      remarkPlugins={markdownRemarkPlugins}
      components={{ table: MarkdownTable }}
      className={cn(
        'wotbot-markdown prose prose-sm max-w-none break-words',
        '[&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:bg-muted [&_pre]:p-3',
        '[&_code]:text-[0.9em]',
      )}
    />
  );
}

/**
 * Renders one node of the grouped part tree.
 *
 * `group-thought` is the single collapsed block per turn; the answer text sits
 * outside it, and artifact-producing tools are pulled out by `wotbotGroupBy` so
 * their output stays visible.
 */
function AssistantParts() {
  return (
    // indicator="always": the thought block deliberately never animates, so this
    // is the turn's single activity signal. The default "no-text" mode hides it
    // while the last part is text *or reasoning* -- which left a long
    // reasoning-only phase looking idle with the reasoning collapsed out of view.
    <MessagePrimitive.GroupedParts groupBy={wotbotGroupBy} indicator="always">
      {({ part, children }) => {
        switch (part.type) {
          case GROUP_THOUGHT:
            return <ThoughtGroup>{children}</ThoughtGroup>;
          // The runs inside the block need no chrome of their own; the rows
          // they hold are already uniform and the block owns the spacing.
          case GROUP_REASONING:
          case GROUP_TOOL:
            return children;
          case 'reasoning':
            return <ReasoningPart />;
          case 'tool-call':
            return isStandalonePart(part.toolName) ? (
              <StandaloneToolCall {...part} />
            ) : (
              <GroupedToolCall {...part} />
            );
          case 'text':
            return <MarkdownText />;
          case 'indicator':
            return (
              <ThinkingIndicator
                aria-live="polite"
                label="Thinking"
                role="status"
              />
            );
          default:
            return null;
        }
      }}
    </MessagePrimitive.GroupedParts>
  );
}

function BranchPicker({ className }: { className?: string }) {
  return (
    <BranchPickerPrimitive.Root
      hideWhenSingleBranch
      className={cn(
        'flex items-center gap-1 text-xs text-muted-foreground',
        className,
      )}
    >
      <BranchPickerPrimitive.Previous asChild>
        <Button aria-label="Previous version" size="icon" variant="ghost">
          <ChevronLeft className="size-3.5" />
        </Button>
      </BranchPickerPrimitive.Previous>
      <span className="tabular-nums">
        <BranchPickerPrimitive.Number /> / <BranchPickerPrimitive.Count />
      </span>
      <BranchPickerPrimitive.Next asChild>
        <Button aria-label="Next version" size="icon" variant="ghost">
          <ChevronRight className="size-3.5" />
        </Button>
      </BranchPickerPrimitive.Next>
    </BranchPickerPrimitive.Root>
  );
}

function UserMessage() {
  return (
    <MessagePrimitive.Root className="wotbot-message flex w-full flex-col items-end py-2">
      <div className="flex w-full items-center justify-end gap-1">
        {/* Editing forks the thread server-side, so the old answer is replaced
            rather than left sitting next to the new one. */}
        <ActionBarPrimitive.Root
          autohide="never"
          className="wotbot-message-actions"
        >
          <ActionBarPrimitive.Edit asChild>
            <Button aria-label="Edit message" size="icon" variant="ghost">
              <Pencil className="size-3.5" />
            </Button>
          </ActionBarPrimitive.Edit>
        </ActionBarPrimitive.Root>

        <div className="min-w-0 max-w-[80%] rounded-lg bg-muted px-4 py-2 text-sm leading-6 text-foreground">
          <MessagePrimitive.Parts />
        </div>
      </div>
      <BranchPicker className="mt-1 mr-1" />
    </MessagePrimitive.Root>
  );
}

function EditComposer() {
  return (
    <ComposerPrimitive.Root className="my-2 w-full rounded-lg border border-border bg-background p-2">
      <ComposerPrimitive.Input
        autoFocus
        className="max-h-40 min-h-16 w-full resize-none bg-transparent text-sm outline-none"
      />
      <div className="flex justify-end gap-2 pt-2">
        <ComposerPrimitive.Cancel asChild>
          <Button size="sm" variant="ghost">
            Cancel
          </Button>
        </ComposerPrimitive.Cancel>
        <ComposerPrimitive.Send asChild>
          <Button size="sm">Save</Button>
        </ComposerPrimitive.Send>
      </div>
    </ComposerPrimitive.Root>
  );
}

/** Run-lifecycle lines in job transcripts; never present in chat threads. */
function SystemMessage() {
  return (
    <MessagePrimitive.Root className="flex w-full justify-center py-1">
      <div className="text-xs text-muted-foreground">
        <MessagePrimitive.Parts />
      </div>
    </MessagePrimitive.Root>
  );
}

function AssistantMessage() {
  // `ThreadPrimitive.Messages` renders by index, so the boundary instance is
  // reused for whatever message later occupies the slot. Keyed by message id it
  // remounts instead of leaving one bad part's failure stuck to position N --
  // the same reuse hazard `ThoughtGroup` deregisters for.
  const messageId = useAuiState((state) => state.message.id ?? '');

  return (
    <MessagePrimitive.Root
      id={messageAnchor(messageId)}
      tabIndex={-1}
      className="wotbot-message flex w-full scroll-mt-3 flex-col items-start rounded-lg py-2 focus-visible:outline-2 focus-visible:outline-ring"
    >
      <div className="w-full min-w-0 text-foreground">
        <ErrorBoundary
          key={messageId}
          label="AssistantMessage"
          fallback={
            <p className="text-sm text-muted-foreground italic">
              This message could not be displayed.
            </p>
          }
        >
          <AssistantParts />
        </ErrorBoundary>
      </div>
      <div className="mt-1 flex items-center gap-1">
        <AuiIf condition={hasAssistantResponseActions}>
          <ActionBarPrimitive.Root
            autohide="never"
            className="wotbot-message-actions flex items-center gap-1"
          >
            <ActionBarPrimitive.Copy asChild>
              <Button aria-label="Copy response" size="icon" variant="ghost">
                <Copy className="size-3.5" />
              </Button>
            </ActionBarPrimitive.Copy>
            <AuiIf condition={hasAssistantReloadAction}>
              <ActionBarPrimitive.Reload asChild>
                <Button
                  aria-label="Regenerate response"
                  size="icon"
                  variant="ghost"
                >
                  <RefreshCw className="size-3.5" />
                </Button>
              </ActionBarPrimitive.Reload>
            </AuiIf>
          </ActionBarPrimitive.Root>
        </AuiIf>
        <BranchPicker />
      </div>
    </MessagePrimitive.Root>
  );
}

export function ThreadErrorNotice({
  className,
  message,
  onRetry,
  retrying = false,
}: {
  className?: string;
  message: string;
  onRetry: () => void;
  retrying?: boolean;
}) {
  return (
    <div
      className={cn(
        'flex items-center justify-between gap-3 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-foreground',
        className,
      )}
      role="alert"
    >
      <div className="flex min-w-0 items-center gap-2">
        <CircleAlert className="size-4 shrink-0 text-destructive" />
        <span>{message}</span>
      </div>
      <Button
        disabled={retrying}
        onClick={onRetry}
        size="sm"
        type="button"
        variant="outline"
      >
        {retrying ? (
          <LoaderCircle className="size-3.5 animate-spin" />
        ) : (
          <RefreshCw className="size-3.5" />
        )}
        Reload
      </Button>
    </div>
  );
}

export function WotbotThread({
  actionSlot,
  className,
  emptyState,
  emptyComposerSlot,
  error,
  footer,
  isRetrying,
  onRetry,
  pendingSlot,
  placeholder = 'Type your message...',
  rerunConfirmation,
}: {
  /** Controls rendered immediately before the shared Voice/Send slot. */
  actionSlot?: ReactNode;
  className?: string;
  emptyState?: ReactNode;
  /** Replaces Send while the composer has no draft (for example, Live mode). */
  emptyComposerSlot?: ReactNode;
  error?: string | null;
  /** Replaces the composer entirely, for read-only transcripts. */
  footer?: ReactNode;
  isRetrying?: boolean;
  onRetry?: () => void;
  /**
   * Rendered as the last item of the transcript, after the messages.
   *
   * A suspended run's prompt belongs where the turn that raised it ended, so
   * it scrolls with the conversation instead of floating above it.
   */
  pendingSlot?: ReactNode;
  placeholder?: string;
  rerunConfirmation?: {
    deviceChangeCount: number;
    kind: 'edit' | 'regenerate';
    onCancel: () => void;
    onConfirm: () => Promise<void>;
  } | null;
}) {
  const isEmpty = useAuiState((state) => state.thread.isEmpty);
  const shouldReduceMotion = useReducedMotion();
  const deviceChangeCount = rerunConfirmation?.deviceChangeCount ?? 0;
  const rerunAction =
    rerunConfirmation?.kind === 'edit'
      ? 'Saving this edit'
      : 'Regenerating this response';

  return (
    <ThreadPrimitive.Root
      className={cn(
        'grid min-h-0 grid-cols-1 grid-rows-[minmax(0,1fr)_auto]',
        className,
      )}
    >
      {rerunConfirmation ? (
        <ConfirmDialog
          confirmLabel={
            rerunConfirmation.kind === 'edit'
              ? 'Save and run again'
              : 'Regenerate anyway'
          }
          description={`${rerunAction} runs the turn again and may repeat ${deviceChangeCount} successful device-changing interaction${deviceChangeCount === 1 ? '' : 's'}. Continue only if repeating those changes is safe.`}
          destructive
          onConfirm={rerunConfirmation.onConfirm}
          onOpenChange={(open) => {
            if (!open) rerunConfirmation.onCancel();
          }}
          open
          title="Repeat device changes?"
        />
      ) : null}

      <ThreadPrimitive.Viewport className="relative col-start-1 row-start-1 flex min-h-0 flex-col overflow-y-auto px-3">
        {emptyState ? (
          <ThreadPrimitive.Empty>{emptyState}</ThreadPrimitive.Empty>
        ) : null}

        <div className="mx-auto w-full max-w-3xl flex-1">
          <ThreadPrimitive.Messages
            components={{
              UserMessage,
              AssistantMessage,
              SystemMessage,
              EditComposer,
            }}
          />
          {pendingSlot}
        </div>

        <ThreadPrimitive.ScrollToBottom asChild>
          <Button
            aria-label="Scroll to bottom"
            className="sticky bottom-2 self-center rounded-full shadow-md disabled:invisible"
            size="icon"
            variant="outline"
          >
            <ArrowDown className="size-4" />
          </Button>
        </ThreadPrimitive.ScrollToBottom>
      </ThreadPrimitive.Viewport>

      {footer ? (
        <div className="col-start-1 row-start-2">{footer}</div>
      ) : (
        <motion.div
          initial={false}
          layout="position"
          transition={
            shouldReduceMotion
              ? { layout: { duration: 0 } }
              : {
                  layout: {
                    duration: 0.55,
                    ease: [0.22, 1, 0.36, 1],
                  },
                }
          }
          className={cn(
            'relative z-10 col-start-1 w-full',
            isEmpty ? 'row-start-1 self-center' : 'row-start-2 self-end',
          )}
        >
          {error && onRetry ? (
            <ThreadErrorNotice
              className="mx-auto mb-2 w-[calc(100%-1.5rem)] max-w-3xl"
              message={error}
              onRetry={onRetry}
              retrying={isRetrying}
            />
          ) : null}

          <ComposerPrimitive.Root className="mx-auto w-full max-w-3xl px-3 pb-3">
            <div className="rounded-2xl border border-border bg-background/95 px-3 py-2 shadow-sm transition-[border-color,box-shadow] focus-within:border-ring/60 focus-within:shadow-md">
              <ComposerPrimitive.Input
                autoFocus
                className="max-h-40 min-h-16 w-full resize-none bg-transparent px-1 py-1 text-sm outline-none placeholder:text-muted-foreground"
                placeholder={placeholder}
                rows={2}
              />
              <div className="flex items-center justify-end gap-2 border-t border-border/80 pt-2">
                <ConversationFiles />
                {/* Send and Stop share a slot: the primitives render whichever
                  matches the thread's running state. */}
                <div className="flex items-center gap-2">
                  <ThreadPrimitive.If running={false}>
                    {actionSlot}
                    {emptyComposerSlot ? (
                      <>
                        <AuiIf condition={hasDraft}>
                          <ComposerPrimitive.Send asChild>
                            <Button type="submit">Send</Button>
                          </ComposerPrimitive.Send>
                        </AuiIf>
                        <AuiIf condition={(state) => !hasDraft(state)}>
                          {emptyComposerSlot}
                        </AuiIf>
                      </>
                    ) : (
                      <ComposerPrimitive.Send asChild>
                        <Button type="submit">Send</Button>
                      </ComposerPrimitive.Send>
                    )}
                  </ThreadPrimitive.If>
                  <ThreadPrimitive.If running>
                    <ComposerPrimitive.Cancel asChild>
                      <Button variant="secondary" type="button">
                        <Square className="mr-1 size-3" />
                        Stop
                      </Button>
                    </ComposerPrimitive.Cancel>
                  </ThreadPrimitive.If>
                </div>
              </div>
            </div>
          </ComposerPrimitive.Root>
        </motion.div>
      )}
    </ThreadPrimitive.Root>
  );
}
