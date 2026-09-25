'use client';

import { AssistantRuntimeProvider } from '@assistant-ui/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  ThreadErrorNotice,
  WotbotThread,
} from '@/components/wotbot/assistant/thread';
import {
  useThreadHistory,
  useWotbotRuntime,
  type ThreadHistory,
} from '@/components/wotbot/assistant/use-wotbot-runtime';
import { LiveModePanel } from '@/components/wotbot/live-mode-panel';
import { MediaIngressControl } from '@/components/wotbot/media-ingress-control';
import { WelcomeScreen } from '@/components/wotbot/welcome-screen';
import { useMediaIngressSession } from '@/hooks/use-media-ingress-session';
import {
  type EmbedChatPrefill,
  normalizeEmbedPrefillPrompt,
} from '@/lib/embed-chat';
import { useTheme, type Theme } from '@/components/theme-provider';

type EmbedChatPrefillRequest = EmbedChatPrefill & {
  id: number;
};

const PREFILL_DEDUPE_WINDOW_MS = 1000;

function isDeckPrefillMessage(
  data: unknown,
): data is { prompt: unknown; submit?: unknown; type: 'deck:prefill' } {
  return (
    typeof data === 'object' &&
    data !== null &&
    'type' in data &&
    data.type === 'deck:prefill'
  );
}

function getDeckPrefill(data: unknown): EmbedChatPrefill | null {
  if (!isDeckPrefillMessage(data)) {
    return null;
  }

  const prompt = normalizeEmbedPrefillPrompt(data.prompt);
  if (!prompt) {
    return null;
  }

  return {
    prompt,
    submit: data.submit === true,
  };
}

/**
 * Owns the stream, mounted only once history has loaded: `useStream` latches
 * `initialValues` at mount and will not adopt a later change.
 */
function EmbedStream({
  appliedPrefillIdsRef,
  chatId,
  history,
  mediaSession,
  prefillRequest,
  reasoningEffort,
  submittedPrefillIdsRef,
}: {
  appliedPrefillIdsRef: { current: Set<number> };
  chatId: string;
  history: ThreadHistory;
  mediaSession: ReturnType<typeof useMediaIngressSession>;
  prefillRequest: EmbedChatPrefillRequest | null;
  reasoningEffort: string | null;
  submittedPrefillIdsRef: { current: Set<number> };
}) {
  const {
    isRecovering,
    rerunConfirmation,
    retryRecovery,
    runError,
    runtime,
    stream,
    submitText,
  } = useWotbotRuntime({
    threadId: chatId,
    initialValues: history.values,
    reasoningEffort: reasoningEffort ?? undefined,
  });

  // Applies a queued prefill to the composer, and submits it when asked.
  useEffect(() => {
    if (!prefillRequest) {
      return;
    }

    if (!appliedPrefillIdsRef.current.has(prefillRequest.id)) {
      appliedPrefillIdsRef.current.add(prefillRequest.id);
      runtime.thread.composer.setText(prefillRequest.prompt);
    }

    if (
      prefillRequest.submit &&
      !stream.isLoading &&
      !submittedPrefillIdsRef.current.has(prefillRequest.id)
    ) {
      submittedPrefillIdsRef.current.add(prefillRequest.id);
      runtime.thread.composer.setText('');
      submitText(prefillRequest.prompt);
    }
  }, [
    appliedPrefillIdsRef,
    prefillRequest,
    runtime,
    stream.isLoading,
    submitText,
    submittedPrefillIdsRef,
  ]);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <WotbotThread
        className="wotbot-chat embed-chat-frame flex-1"
        emptyState={<WelcomeScreen historyLoaded />}
        emptyComposerSlot={<MediaIngressControl session={mediaSession} />}
        error={runError}
        isRetrying={isRecovering}
        onRetry={() => void retryRecovery()}
        rerunConfirmation={rerunConfirmation}
      />
    </AssistantRuntimeProvider>
  );
}

/** Waits for history before mounting the stream, which latches it at mount. */
function EmbedSurface({
  appliedPrefillIdsRef,
  chatId,
  mediaSession,
  onHistorySettled,
  prefillRequest,
  reasoningEffort,
  settleAfterLive,
  submittedPrefillIdsRef,
}: {
  appliedPrefillIdsRef: { current: Set<number> };
  chatId: string;
  mediaSession: ReturnType<typeof useMediaIngressSession>;
  onHistorySettled: () => void;
  prefillRequest: EmbedChatPrefillRequest | null;
  reasoningEffort: string | null;
  settleAfterLive: boolean;
  submittedPrefillIdsRef: { current: Set<number> };
}) {
  const history = useThreadHistory(chatId, {
    onSettled: onHistorySettled,
    settleAfterLive,
  });

  if (!history.loaded) {
    return (
      <div className="wotbot-chat embed-chat-frame flex min-h-0 flex-1 flex-col">
        <WelcomeScreen historyLoaded={false} />
      </div>
    );
  }
  if (history.error) {
    return (
      <div className="wotbot-chat embed-chat-frame grid min-h-0 flex-1 place-items-center px-3">
        <ThreadErrorNotice
          className="w-full max-w-xl"
          message={history.error}
          onRetry={history.reload}
        />
      </div>
    );
  }

  return (
    <EmbedStream
      appliedPrefillIdsRef={appliedPrefillIdsRef}
      chatId={chatId}
      history={history}
      mediaSession={mediaSession}
      prefillRequest={prefillRequest}
      reasoningEffort={reasoningEffort}
      submittedPrefillIdsRef={submittedPrefillIdsRef}
    />
  );
}

function EmbedThemeOverride({ theme }: { theme: Theme | null }) {
  const { setForcedTheme } = useTheme();

  useEffect(() => {
    setForcedTheme(theme);
    return () => setForcedTheme(null);
  }, [setForcedTheme, theme]);

  return null;
}

export function EmbedChatExperience({
  allowedPrefillOrigins,
  chatId,
  embedTheme,
  initialPrefill,
  reasoningEffort,
}: {
  allowedPrefillOrigins: string[];
  chatId: string;
  embedTheme: Theme | null;
  initialPrefill: EmbedChatPrefill | null;
  reasoningEffort: string | null;
}) {
  const cleanupRequestedRef = useRef(false);
  const initialPrefillAppliedRef = useRef(false);
  const appliedPrefillIdsRef = useRef<Set<number>>(new Set());
  const lastQueuedPrefillRef = useRef<{
    key: string;
    queuedAt: number;
  } | null>(null);
  const nextPrefillIdRef = useRef(0);
  const submittedPrefillIdsRef = useRef<Set<number>>(new Set());
  const [prefillRequest, setPrefillRequest] =
    useState<EmbedChatPrefillRequest | null>(null);
  const [historyVersion, setHistoryVersion] = useState(0);
  const [settleHistoryChatId, setSettleHistoryChatId] = useState<string | null>(
    null,
  );
  const mediaSession = useMediaIngressSession(chatId);
  const handleHistorySettled = useCallback(() => {
    setSettleHistoryChatId((current) => (current === chatId ? null : current));
  }, [chatId]);

  const allowedPrefillOriginSet = useMemo(
    () => new Set(allowedPrefillOrigins),
    [allowedPrefillOrigins],
  );

  const isAllowedPrefillOrigin = useCallback(
    (origin: string) => {
      return (
        origin === window.location.origin || allowedPrefillOriginSet.has(origin)
      );
    },
    [allowedPrefillOriginSet],
  );

  const queuePrefill = useCallback((prefill: EmbedChatPrefill) => {
    const prompt = normalizeEmbedPrefillPrompt(prefill.prompt);
    if (!prompt) {
      return;
    }

    const key = `${prefill.submit ? 'submit' : 'prefill'}:${prompt}`;
    const now = Date.now();
    if (
      lastQueuedPrefillRef.current?.key === key &&
      now - lastQueuedPrefillRef.current.queuedAt < PREFILL_DEDUPE_WINDOW_MS
    ) {
      return;
    }

    lastQueuedPrefillRef.current = { key, queuedAt: now };
    nextPrefillIdRef.current += 1;
    setPrefillRequest({
      id: nextPrefillIdRef.current,
      prompt,
      submit: prefill.submit,
    });
  }, []);

  useEffect(() => {
    if (!initialPrefill || initialPrefillAppliedRef.current) {
      return;
    }

    initialPrefillAppliedRef.current = true;
    queuePrefill(initialPrefill);
  }, [initialPrefill, queuePrefill]);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (window.parent === window || event.source !== window.parent) {
        return;
      }

      if (!isAllowedPrefillOrigin(event.origin)) {
        return;
      }

      const prefill = getDeckPrefill(event.data);
      if (!prefill) {
        return;
      }

      queuePrefill(prefill);
    };

    window.addEventListener('message', onMessage);

    return () => window.removeEventListener('message', onMessage);
  }, [isAllowedPrefillOrigin, queuePrefill]);

  useEffect(() => {
    const cleanupSession = () => {
      if (cleanupRequestedRef.current) {
        return;
      }

      cleanupRequestedRef.current = true;
      void fetch(`/api/chats/${encodeURIComponent(chatId)}`, {
        method: 'DELETE',
        keepalive: true,
      }).catch(() => {
        // Ephemeral embed cleanup is best-effort only.
      });
    };

    window.addEventListener('pagehide', cleanupSession);
    window.addEventListener('beforeunload', cleanupSession);

    return () => {
      window.removeEventListener('pagehide', cleanupSession);
      window.removeEventListener('beforeunload', cleanupSession);
    };
  }, [chatId]);

  const showLiveMode = mediaSession.state !== 'idle';

  // LiveKit writes turns through the voice worker, outside this component's
  // stream. Remount the history loader after a call so the text thread adopts
  // those turns instead of reopening with the pre-call snapshot.
  const [wasLiveMode, setWasLiveMode] = useState(showLiveMode);
  if (wasLiveMode !== showLiveMode) {
    setWasLiveMode(showLiveMode);
    if (!showLiveMode) {
      setSettleHistoryChatId(chatId);
      setHistoryVersion((version) => version + 1);
    }
  }

  return (
    <main className="embed-chat-shell flex h-dvh flex-col px-3 py-3 md:px-6 md:py-6">
      <EmbedThemeOverride theme={embedTheme} />
      <div className="mx-auto flex min-h-0 w-full max-w-5xl flex-1 flex-col">
        {showLiveMode ? (
          <div className="wotbot-chat embed-chat-frame flex min-h-0 flex-1 flex-col">
            <LiveModePanel session={mediaSession} />
          </div>
        ) : (
          <EmbedSurface
            key={`${chatId}:${historyVersion}`}
            appliedPrefillIdsRef={appliedPrefillIdsRef}
            chatId={chatId}
            mediaSession={mediaSession}
            onHistorySettled={handleHistorySettled}
            prefillRequest={prefillRequest}
            reasoningEffort={reasoningEffort}
            settleAfterLive={settleHistoryChatId === chatId}
            submittedPrefillIdsRef={submittedPrefillIdsRef}
          />
        )}
      </div>
    </main>
  );
}
