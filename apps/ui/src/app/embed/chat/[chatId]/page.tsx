import { EmbedChatPage as EmbedChatExperiencePage } from '@/components/wotbot/chat-route-page';
import {
  getEmbedInitialPrefillFromSearchParams,
  getEmbedReasoningEffortFromSearchParams,
  getEmbedThemeFromSearchParams,
  type AppPageSearchParams,
  toSearchParamsString,
} from '@/lib/embed-chat-search-params';
import { getEmbedChatAllowedOrigins } from '@/lib/embed-chat-runtime-config';
import { getReasoningEffortRuntimeConfig } from '@/lib/reasoning-effort-runtime-config';

export const dynamic = 'force-dynamic';

export default async function EmbedChatThreadPage({
  searchParams,
}: {
  searchParams: Promise<AppPageSearchParams>;
}) {
  const resolvedSearchParams = await searchParams;

  return (
    <EmbedChatExperiencePage
      allowedPrefillOrigins={getEmbedChatAllowedOrigins()}
      embedQueryString={toSearchParamsString(resolvedSearchParams)}
      initialPrefill={getEmbedInitialPrefillFromSearchParams(
        resolvedSearchParams,
      )}
      embedTheme={getEmbedThemeFromSearchParams(resolvedSearchParams)}
      reasoningEffort={getEmbedReasoningEffortFromSearchParams(
        resolvedSearchParams,
        getReasoningEffortRuntimeConfig(),
      )}
    />
  );
}
