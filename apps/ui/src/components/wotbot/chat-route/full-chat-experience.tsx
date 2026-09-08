'use client';

import { AssistantRuntimeProvider } from '@assistant-ui/react';
import { MessageSquarePlus } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';

import { AppSidebar } from '@/components/chat-sidebar';
import { latestTurnArtifacts } from '@/components/wotbot/assistant/artifacts';
import {
  ThreadErrorNotice,
  WotbotThread,
} from '@/components/wotbot/assistant/thread';
import {
  useThreadHistory,
  useWotbotRuntime,
} from '@/components/wotbot/assistant/use-wotbot-runtime';
import { parseCredentialChallenge } from '@/components/wotbot/assistant/credential-interrupt-card';
import {
  CredentialPrompt,
  SourceRegistrationPrompt,
} from '@/components/wotbot/assistant/interrupt-prompt';
import { parseSourceRegistrationInterrupt } from '@/components/wotbot/assistant/source-registration-interrupt-card';
import { ReasoningEffortSelect } from '@/components/wotbot/chat-route/reasoning-effort-select';
import { LiveModePanel } from '@/components/wotbot/live-mode-panel';
import { MediaIngressControl } from '@/components/wotbot/media-ingress-control';
import { WelcomeScreen } from '@/components/wotbot/welcome-screen';
import { SiteHeader } from '@/components/site-header';
import { Button } from '@/components/ui/button';
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar';
import {
  useMediaIngressSession,
  type MediaIngressSession,
} from '@/hooks/use-media-ingress-session';
import { type ChatSummary } from '@/lib/chat-list-cache';
import type { ThreadHistory } from '@/components/wotbot/assistant/use-wotbot-runtime';
import type { LangChainMessage } from '@/lib/thread-messages';
import type { ReasoningEffortConfig } from '@/lib/reasoning-effort';

/**
 * Owns the stream for one thread.
 *
 * Split out so it can be remounted by key: `useStream` holds the message list,
 * and after live mode has appended turns through LiveKit the only way to adopt
 * them is to rebuild the stream from freshly loaded history.
 */
function ChatStream({
  chatId,
  initialValues,
  mediaSession,
  onLevelChange,
  onThreadUpdated,
  reasoningEffort,
  reasoningEffortConfig,
}: {
  chatId: string;
  initialValues: ThreadHistory['values'];
  mediaSession: MediaIngressSession;
  onLevelChange: (level: string | null) => void;
  onThreadUpdated: () => void;
  reasoningEffort: string | undefined;
  reasoningEffortConfig: ReasoningEffortConfig;
}) {
  const {
    isRecovering,
    rerunConfirmation,
    retryRecovery,
    runError,
    runtime,
    stream,
  } = useWotbotRuntime({
    threadId: chatId,
    initialValues,
    reasoningEffort,
    onThreadUpdated,
  });
  const credentialChallenge = useMemo(
    () =>
      stream.interrupts
        .map((item) => parseCredentialChallenge(item.value))
        .find((item) => item !== null) ?? null,
    [stream.interrupts],
  );
  const sourceRegistration = useMemo(
    () =>
      stream.interrupts
        .map((item) => parseSourceRegistrationInterrupt(item.value))
        .find((item) => item !== null) ?? null,
    [stream.interrupts],
  );
  const resumeCredential = useCallback(
    async (status: 'credential_saved' | 'credential_cancelled') => {
      await stream.submit(null, { command: { resume: { status } } });
    },
    [stream],
  );

  // The run is suspended until one of these is answered, so the prompt sits at
  // the end of the transcript rather than above it.
  const pendingSlot = credentialChallenge ? (
    <CredentialPrompt
      challenge={credentialChallenge}
      onCancel={() => resumeCredential('credential_cancelled')}
      onSaved={() => resumeCredential('credential_saved')}
    />
  ) : sourceRegistration ? (
    <SourceRegistrationPrompt
      draft={sourceRegistration.draft}
      onCancel={() =>
        stream.submit(null, {
          command: { resume: { status: 'source_registration_cancelled' } },
        })
      }
      onRegistered={(sourceId, thingId) =>
        stream.submit(null, {
          command: {
            resume: {
              status: 'source_registered',
              source_id: sourceId,
              ...(thingId ? { thing_id: thingId } : {}),
            },
          },
        })
      }
    />
  ) : null;

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <WotbotThread
        pendingSlot={pendingSlot}
        actionSlot={
          <ReasoningEffortSelect
            config={reasoningEffortConfig}
            onLevelChange={onLevelChange}
            value={reasoningEffort}
          />
        }
        className="wotbot-chat flex-1"
        emptyState={<WelcomeScreen historyLoaded />}
        emptyComposerSlot={<MediaIngressControl session={mediaSession} />}
        error={runError}
        isRetrying={isRecovering}
        onRetry={() => void retryRecovery()}
        placeholder={
          pendingSlot
            ? 'Answer the request above to continue...'
            : 'Ask about your Things, data, or automations...'
        }
        rerunConfirmation={rerunConfirmation}
      />
    </AssistantRuntimeProvider>
  );
}

/** Waits for history before mounting the stream, which latches it at mount. */
function ChatSurface({
  onHistorySettled,
  settleAfterLive,
  ...streamProps
}: {
  chatId: string;
  mediaSession: MediaIngressSession;
  onHistorySettled: () => void;
  onLevelChange: (level: string | null) => void;
  onThreadUpdated: () => void;
  reasoningEffort: string | undefined;
  reasoningEffortConfig: ReasoningEffortConfig;
  settleAfterLive: boolean;
}) {
  const history = useThreadHistory(streamProps.chatId, {
    onSettled: onHistorySettled,
    settleAfterLive,
  });

  if (!history.loaded) {
    return <WelcomeScreen historyLoaded={false} />;
  }
  if (history.error) {
    return (
      <div className="grid min-h-0 flex-1 place-items-center px-3">
        <ThreadErrorNotice
          className="w-full max-w-xl"
          message={history.error}
          onRetry={history.reload}
        />
      </div>
    );
  }

  return <ChatStream {...streamProps} initialValues={history.values} />;
}

export function FullChatExperience({
  chatId,
  handleNewChat,
  reasoningEffortConfig,
}: {
  chatId: string;
  handleNewChat: () => Promise<ChatSummary | null>;
  reasoningEffortConfig: ReasoningEffortConfig;
}) {
  const [sidebarRefreshToken, setSidebarRefreshToken] = useState(0);
  const [historyVersion, setHistoryVersion] = useState(0);
  // Owned here, not in ChatStream: leaving live mode remounts the chat surface,
  // and state living below that boundary would be discarded on every exit.
  const [reasoningEffort, setReasoningEffort] = useState<string | undefined>();
  const [settleHistoryChatId, setSettleHistoryChatId] = useState<string | null>(
    null,
  );
  const [liveHistory, setLiveHistory] = useState<{
    chatId: string;
    messages: LangChainMessage[];
  } | null>(null);
  const mediaSession = useMediaIngressSession(chatId);

  const handleSidebarRefresh = useCallback(() => {
    setSidebarRefreshToken((current) => current + 1);
  }, []);
  const handleLevelChange = useCallback((level: string | null) => {
    setReasoningEffort(level ?? undefined);
  }, []);
  const handleHistorySettled = useCallback(() => {
    setSettleHistoryChatId((current) => (current === chatId ? null : current));
  }, [chatId]);

  const breadcrumbs = useMemo(() => [{ label: 'Chat', href: '/chat' }], []);
  const showLiveMode = mediaSession.state !== 'idle';

  // Adjust-during-render rather than an effect: leaving live mode must remount
  // the chat surface so it reloads the turns LiveKit appended, and doing that
  // in an effect would render the stale thread for a frame first.
  const [wasLiveMode, setWasLiveMode] = useState(showLiveMode);
  if (wasLiveMode !== showLiveMode) {
    setWasLiveMode(showLiveMode);
    if (!showLiveMode) {
      setSettleHistoryChatId(chatId);
      setHistoryVersion((version) => version + 1);
    }
  }
  const liveMessages = useMemo(
    () => (liveHistory?.chatId === chatId ? liveHistory.messages : []),
    [chatId, liveHistory],
  );
  const liveArtifacts = useMemo(
    () => latestTurnArtifacts(liveMessages),
    [liveMessages],
  );

  // Live mode drives the same thread over LiveKit rather than through this
  // stream, so its turns are only visible by re-reading the thread. Poll while
  // it runs to keep the artifact panel current, then remount the chat surface
  // on exit so the text thread adopts everything that was said.
  useEffect(() => {
    if (!showLiveMode) {
      return;
    }

    let cancelled = false;

    const poll = () => {
      void fetch(`/api/chat/${encodeURIComponent(chatId)}/state`)
        .then((response) => (response.ok ? response.json() : null))
        .then((data: { values?: { messages?: LangChainMessage[] } } | null) => {
          if (!cancelled) {
            setLiveHistory({
              chatId,
              messages: data?.values?.messages ?? [],
            });
          }
        })
        .catch(() => {
          // Best-effort: a missed poll just delays the artifact panel.
        });
    };

    poll();
    const interval = window.setInterval(poll, 2500);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [chatId, showLiveMode]);

  return (
    <SidebarProvider className="relative h-dvh overflow-hidden text-foreground">
      <AppSidebar
        activeChatId={chatId}
        onNewChat={handleNewChat}
        refreshToken={sidebarRefreshToken}
      />

      <SidebarInset>
        <SiteHeader breadcrumbs={breadcrumbs}>
          <Button
            className="md:hidden"
            onClick={() => void handleNewChat()}
            size="sm"
            type="button"
            variant="outline"
          >
            <MessageSquarePlus className="size-4" />
            <span>New chat</span>
          </Button>
        </SiteHeader>

        <div className="flex min-h-0 flex-1 flex-col p-3 md:p-4">
          {showLiveMode ? (
            <LiveModePanel artifacts={liveArtifacts} session={mediaSession} />
          ) : (
            <ChatSurface
              key={`${chatId}:${historyVersion}`}
              chatId={chatId}
              mediaSession={mediaSession}
              onHistorySettled={handleHistorySettled}
              onLevelChange={handleLevelChange}
              onThreadUpdated={handleSidebarRefresh}
              reasoningEffort={reasoningEffort}
              reasoningEffortConfig={reasoningEffortConfig}
              settleAfterLive={settleHistoryChatId === chatId}
            />
          )}
        </div>
      </SidebarInset>
    </SidebarProvider>
  );
}
