'use client';

import {
  useExternalStoreRuntime,
  type ThreadMessageLike,
} from '@assistant-ui/react';
import {
  FetchStreamTransport,
  useStream,
} from '@langchain/langgraph-sdk/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { toast } from 'sonner';

import {
  countDeviceChangesForRetry,
  findRetryTarget,
  type RetryTarget,
} from '@/components/wotbot/assistant/message-actions';
import {
  buildTextSubmission,
  type WotbotState,
} from '@/lib/chat-runtime-state';
import {
  LIVE_MODE_SETTLE_DELAYS_MS,
  RUN_RECOVERY_DELAYS_MS,
  loadSettledThreadState,
} from '@/lib/thread-state';
import {
  createThreadMessageConverter,
  identityThreadMessage,
} from '@/lib/thread-messages';

function appendedText(content: readonly { type: string }[]): string {
  return content
    .filter(
      (part): part is { type: 'text'; text: string } => part.type === 'text',
    )
    .map((part) => part.text)
    .join('\n')
    .trim();
}

/**
 * Bridges LangGraph's `useStream` into assistant-ui.
 *
 * `useStream` owns the transport and the message state; assistant-ui only
 * renders. `useExternalStoreRuntime` reflects state we already hold rather than
 * keeping its own copy, so there is no second source of truth to reconcile and
 * nothing that can synthesize an event out of order.
 */
export type ThreadHistory = {
  error: string | null;
  loaded: boolean;
  reload: () => void;
  values: WotbotState;
};

const EMPTY_HISTORY: WotbotState = { messages: [] };

type PendingRerun = RetryTarget & {
  deviceChangeCount: number;
  kind: 'edit' | 'regenerate';
};

/**
 * Loads a thread's persisted state.
 *
 * Kept separate from the runtime because `useStream` reads `initialValues`
 * when it mounts and does not adopt a later change: seeding it after the fetch
 * resolves leaves the thread permanently empty. Callers therefore wait for
 * `loaded` before mounting the runtime.
 *
 * A custom transport has no `fetchStateHistory`, so this is where history
 * comes from at all.
 */
function normalizeWotbotState(values: Partial<WotbotState>): WotbotState {
  return {
    ...values,
    messages: Array.isArray(values.messages) ? values.messages : [],
  };
}

export function useThreadHistory(
  threadId: string,
  {
    onSettled,
    settleAfterLive = false,
  }: { onSettled?: () => void; settleAfterLive?: boolean } = {},
): ThreadHistory {
  // Keyed by threadId so switching threads derives "not loaded yet" rather
  // than resetting state imperatively inside the effect.
  const [history, setHistory] = useState<{
    error: string | null;
    revision: number;
    threadId: string;
    values: WotbotState;
  } | null>(null);
  const [revision, setRevision] = useState(0);
  // A keyed history surface mounts specifically for one post-live reload. Do
  // not restart its request when the parent clears that one-shot flag.
  const settleAfterLiveRef = useRef(settleAfterLive);
  const reload = useCallback(() => setRevision((current) => current + 1), []);

  useEffect(() => {
    let cancelled = false;

    void loadSettledThreadState<WotbotState>(threadId, {
      delaysMs: settleAfterLiveRef.current ? LIVE_MODE_SETTLE_DELAYS_MS : [0],
    })
      .then((values) => {
        if (cancelled) return;
        setHistory({
          error: null,
          revision,
          threadId,
          values: normalizeWotbotState(values),
        });
        if (settleAfterLiveRef.current) {
          onSettled?.();
        }
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setHistory({
          error:
            error instanceof Error
              ? error.message
              : 'Could not load conversation',
          revision,
          threadId,
          values: EMPTY_HISTORY,
        });
      });

    return () => {
      cancelled = true;
    };
  }, [onSettled, revision, threadId]);

  return history?.threadId === threadId && history.revision === revision
    ? {
        error: history.error,
        loaded: true,
        reload,
        values: history.values,
      }
    : { error: null, loaded: false, reload, values: EMPTY_HISTORY };
}

export function useWotbotRuntime({
  threadId,
  initialValues,
  reasoningEffort,
  onThreadUpdated,
}: {
  threadId: string;
  /** Must be the already-loaded history; see `useThreadHistory`. */
  initialValues: WotbotState;
  reasoningEffort?: string;
  onThreadUpdated?: () => void;
}) {
  const recoveryGenerationRef = useRef(0);
  const [isRecovering, setIsRecovering] = useState(false);
  const [reconciledValues, setReconciledValues] = useState<WotbotState | null>(
    null,
  );
  const [runError, setRunError] = useState<string | null>(null);
  const [pendingRerun, setPendingRerun] = useState<PendingRerun | null>(null);

  const reconcileThreadState = useCallback(async () => {
    const generation = recoveryGenerationRef.current + 1;
    recoveryGenerationRef.current = generation;
    setIsRecovering(true);
    try {
      const values = await loadSettledThreadState<WotbotState>(threadId, {
        delaysMs: RUN_RECOVERY_DELAYS_MS,
      });
      if (recoveryGenerationRef.current !== generation) {
        return false;
      }
      setReconciledValues(normalizeWotbotState(values));
      onThreadUpdated?.();
      return true;
    } catch {
      return false;
    } finally {
      if (recoveryGenerationRef.current === generation) {
        setIsRecovering(false);
      }
    }
  }, [onThreadUpdated, threadId]);

  const transport = useMemo(
    () =>
      new FetchStreamTransport<WotbotState>({
        apiUrl: `/api/chat/${encodeURIComponent(threadId)}/stream`,
      }),
    [threadId],
  );

  const stream = useStream<WotbotState>({
    transport,
    threadId,
    initialValues,
    onError: () => {
      setRunError('The response failed. Reload the conversation to try again.');
      void reconcileThreadState();
    },
    onFinish: () => {
      setReconciledValues(null);
      setRunError(null);
      onThreadUpdated?.();
    },
  });

  // `stream.messages` is empty until a run produces values -- `initialValues`
  // seeds what gets submitted, not what is displayed -- so loaded history is
  // what renders until then. Once a run starts, the backend's `values` frames
  // carry the full accumulated state, so the stream becomes authoritative and
  // this falls away.
  const [convertMessages] = useState(createThreadMessageConverter);
  const streamedValues = stream.values as Partial<WotbotState>;
  const sourceMessages =
    reconciledValues?.messages ??
    (Array.isArray(streamedValues.messages)
      ? streamedValues.messages
      : initialValues.messages);
  const messages = useMemo(
    () => convertMessages(sourceMessages),
    [convertMessages, sourceMessages],
  );

  const submitText = useCallback(
    (text: string, replaceFromId?: string | null) => {
      if (!text) return;
      const streamedValues = stream.values as Partial<WotbotState>;
      const currentValues = reconciledValues ?? streamedValues;
      const submission = buildTextSubmission({
        currentValues,
        initialValues,
        messageId: crypto.randomUUID(),
        reasoningEffort,
        replaceFromId,
        text,
      });

      recoveryGenerationRef.current += 1;
      setIsRecovering(false);
      setReconciledValues(null);
      setRunError(null);
      void stream.submit(submission.input, {
        optimisticValues: submission.optimisticValues,
      });
    },
    [initialValues, reasoningEffort, reconciledValues, stream],
  );

  /**
   * Editing a turn must replace it, not append beside it.
   *
   * LangGraph checkpoints are append-only, so the backend forks history to
   * just before the edited message first; the run that follows then continues
   * from that fork. The authoritative `values` frame it emits replaces the
   * client's message list, which is what makes the superseded answer vanish.
   *
   * Keeping the fork explicit also lets a failed rewind stop safely instead of
   * silently appending the edited text as a duplicate turn.
   */
  const editAndResubmit = useCallback(
    async (sourceId: string | null, text: string) => {
      if (!text) return;
      if (sourceId) {
        try {
          const response = await fetch(
            `/api/chat/${encodeURIComponent(threadId)}/fork`,
            {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ message_id: sourceId }),
            },
          );
          const data: unknown = response.ok ? await response.json() : null;
          if (
            !response.ok ||
            typeof data !== 'object' ||
            data === null ||
            !('forked' in data) ||
            data.forked !== true
          ) {
            throw new Error('Thread fork failed');
          }
        } catch {
          toast.error('Could not replace that turn. Please try again.');
          return;
        }
      }
      submitText(text, sourceId);
    },
    [submitText, threadId],
  );

  const rerunWithSafetyCheck = useCallback(
    async ({
      kind,
      parentId,
      target,
    }: {
      kind: PendingRerun['kind'];
      parentId: string | null;
      target: RetryTarget;
    }) => {
      const deviceChangeCount = countDeviceChangesForRetry(messages, parentId);
      if (deviceChangeCount > 0) {
        setPendingRerun({ ...target, deviceChangeCount, kind });
        return;
      }
      await editAndResubmit(target.sourceId, target.text);
    },
    [editAndResubmit, messages],
  );

  const confirmRerun = useCallback(async () => {
    if (!pendingRerun) return;
    await editAndResubmit(pendingRerun.sourceId, pendingRerun.text);
  }, [editAndResubmit, pendingRerun]);

  const dismissRerun = useCallback(() => setPendingRerun(null), []);

  const cancel = useCallback(async () => {
    await stream.stop();
    try {
      const response = await fetch(
        `/api/chat/${encodeURIComponent(threadId)}/cancel`,
        {
          method: 'POST',
        },
      );
      if (!response.ok) {
        throw new Error('Cancellation failed');
      }
    } catch {
      setRunError(
        'The response was stopped locally, but server cancellation could not be confirmed.',
      );
    } finally {
      await reconcileThreadState();
    }
  }, [reconcileThreadState, stream, threadId]);

  const retryRecovery = useCallback(async () => {
    if (await reconcileThreadState()) {
      setRunError(null);
    } else {
      setRunError('Could not reload the conversation. Please try again.');
    }
  }, [reconcileThreadState]);

  const runtime = useExternalStoreRuntime<ThreadMessageLike>({
    messages,
    isRunning: stream.isLoading,
    isSendDisabled: isRecovering,
    convertMessage: identityThreadMessage,
    onNew: async (message) => {
      submitText(appendedText(message.content));
    },
    onEdit: async (message) => {
      const target = {
        sourceId: message.sourceId,
        text: appendedText(message.content),
      };
      await rerunWithSafetyCheck({
        kind: 'edit',
        parentId: message.sourceId,
        target,
      });
    },
    onReload: async (parentId) => {
      const target = findRetryTarget(messages, parentId);
      if (target) {
        await rerunWithSafetyCheck({
          kind: 'regenerate',
          parentId,
          target,
        });
      } else {
        toast.error('Could not find the turn to regenerate.');
      }
    },
    onCancel: cancel,
  });

  return {
    isRecovering,
    rerunConfirmation: pendingRerun
      ? {
          deviceChangeCount: pendingRerun.deviceChangeCount,
          kind: pendingRerun.kind,
          onCancel: dismissRerun,
          onConfirm: confirmRerun,
        }
      : null,
    retryRecovery,
    runError,
    runtime,
    stream,
    submitText,
  };
}
