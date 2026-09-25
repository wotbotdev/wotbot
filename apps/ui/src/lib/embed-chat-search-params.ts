import {
  type EmbedChatPrefill,
  isEmbedAutosubmitValue,
  isEmbedDisabledValue,
  normalizeEmbedPrefillPrompt,
} from './embed-chat';
import type { Theme } from '@/components/theme-provider';
import {
  isReasoningEffortSelectorEnabled,
  type ReasoningEffortConfig,
} from './reasoning-effort';

export type AppPageSearchParams = Record<string, string | string[] | undefined>;
const OMITTED_EMBED_ROUTE_PARAMS = new Set(['examples']);

function getFirstValue(value: string | string[] | undefined): string | null {
  if (typeof value === 'string') {
    return value;
  }

  if (Array.isArray(value)) {
    return value[0] ?? null;
  }

  return null;
}

export function toSearchParamsString(
  searchParams: AppPageSearchParams,
): string {
  const normalized = new URLSearchParams();

  for (const [key, value] of Object.entries(searchParams)) {
    if (OMITTED_EMBED_ROUTE_PARAMS.has(key)) {
      continue;
    }

    if (typeof value === 'string') {
      normalized.set(key, value);
      continue;
    }

    if (!Array.isArray(value)) {
      continue;
    }

    for (const entry of value) {
      normalized.append(key, entry);
    }
  }

  return normalized.toString();
}

export function areEmbedJobEventsEnabledFromSearchParams(
  searchParams: AppPageSearchParams,
): boolean {
  const jobEventsFlag = getFirstValue(searchParams.jobEvents);
  return !isEmbedDisabledValue(jobEventsFlag);
}

export function getEmbedThemeFromSearchParams(
  searchParams: AppPageSearchParams,
): Theme | null {
  const theme = getFirstValue(searchParams.theme)?.trim().toLowerCase();
  return theme === 'light' || theme === 'dark' || theme === 'system'
    ? theme
    : null;
}

export function getEmbedInitialPrefillFromSearchParams(
  searchParams: AppPageSearchParams,
): EmbedChatPrefill | null {
  const prompt = normalizeEmbedPrefillPrompt(
    getFirstValue(searchParams.prompt),
  );
  if (!prompt) {
    return null;
  }

  return {
    prompt,
    submit: isEmbedAutosubmitValue(getFirstValue(searchParams.autosubmit)),
  };
}

/**
 * The reasoning effort an embedded chat submits with every run.
 *
 * The embed has no selector, so the host page picks a level with `effort=`;
 * anything not on the configured list falls back to the configured default,
 * the same level the full chat starts at. Nothing is sent while the feature
 * is disabled, matching the full chat.
 */
export function getEmbedReasoningEffortFromSearchParams(
  searchParams: AppPageSearchParams,
  config: ReasoningEffortConfig,
): string | null {
  if (!isReasoningEffortSelectorEnabled(config)) {
    return null;
  }
  const requested = getFirstValue(searchParams.effort)?.trim();
  return requested && config.levels.includes(requested)
    ? requested
    : config.defaultLevel;
}
