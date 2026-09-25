'use client';

import { useCallback, useState } from 'react';
import { useRouter } from 'next/navigation';

import { createChat } from '@/components/wotbot/chat-route/chat-api';
import { ChatIndexPage } from '@/components/wotbot/chat-route/chat-index-page';
import { EmbedChatExperience } from '@/components/wotbot/chat-route/embed-chat-experience';
import { FullChatExperience } from '@/components/wotbot/chat-route/full-chat-experience';
import {
  createEmbedEphemeralChatId,
  type EmbedChatPrefill,
} from '@/lib/embed-chat';
import type { Theme } from '@/components/theme-provider';
import {
  DISABLED_REASONING_EFFORT_CONFIG,
  type ReasoningEffortConfig,
} from '@/lib/reasoning-effort';

export { ChatIndexPage };

export type ChatRouteMode = 'full' | 'embed';

function toQuerySuffix(queryString: string): string {
  return queryString ? `?${queryString}` : '';
}

export function ChatRoutePage({
  allowedPrefillOrigins = [],
  chatId,
  mode,
  embedQueryString = '',
  embedReasoningEffort = null,
  embedTheme = null,
  initialEmbedPrefill = null,
  reasoningEffortConfig = DISABLED_REASONING_EFFORT_CONFIG,
}: {
  allowedPrefillOrigins?: string[];
  chatId: string;
  mode: ChatRouteMode;
  embedQueryString?: string;
  embedReasoningEffort?: string | null;
  embedTheme?: Theme | null;
  initialEmbedPrefill?: EmbedChatPrefill | null;
  reasoningEffortConfig?: ReasoningEffortConfig;
}) {
  const router = useRouter();
  const querySuffix = toQuerySuffix(embedQueryString);

  const handleNewChat = useCallback(async () => {
    try {
      const chat = await createChat();
      const basePath = mode === 'embed' ? '/embed/chat' : '/chat';
      const suffix = mode === 'embed' ? querySuffix : '';
      router.push(`${basePath}/${chat.id}${suffix}`);
      return chat;
    } catch (error) {
      console.error('Failed to create chat', error);
      return null;
    }
  }, [mode, querySuffix, router]);

  return mode === 'embed' ? (
    <EmbedChatExperience
      allowedPrefillOrigins={allowedPrefillOrigins}
      chatId={chatId}
      embedTheme={embedTheme}
      initialPrefill={initialEmbedPrefill}
      reasoningEffort={embedReasoningEffort}
    />
  ) : (
    <FullChatExperience
      chatId={chatId}
      handleNewChat={handleNewChat}
      reasoningEffortConfig={reasoningEffortConfig}
    />
  );
}

export function EmbedChatPage({
  allowedPrefillOrigins = [],
  embedQueryString = '',
  embedTheme = null,
  initialPrefill = null,
  reasoningEffort = null,
}: {
  allowedPrefillOrigins?: string[];
  embedQueryString?: string;
  embedTheme?: Theme | null;
  initialPrefill?: EmbedChatPrefill | null;
  reasoningEffort?: string | null;
}) {
  const [chatId] = useState(() => createEmbedEphemeralChatId());

  return (
    <ChatRoutePage
      allowedPrefillOrigins={allowedPrefillOrigins}
      chatId={chatId}
      mode="embed"
      embedQueryString={embedQueryString}
      embedReasoningEffort={reasoningEffort}
      embedTheme={embedTheme}
      initialEmbedPrefill={initialPrefill}
    />
  );
}
