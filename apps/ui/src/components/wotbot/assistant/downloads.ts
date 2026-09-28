import { normalizeRunCodeResult } from '@/components/wotbot/chat-tool-call-model';
import { FILE_VIEW_NAME } from '@/lib/thread-messages';

type DownloadPart = { type: string; toolName?: string; result?: unknown };

export type DownloadState = {
  message: { content: readonly DownloadPart[] };
};

/**
 * How many files the turn offers for download, for the collapsed block's
 * counts line. Returns a number because it runs as a `useAuiState` selector.
 */
export function downloadFileCount(state: DownloadState): number {
  let count = 0;
  for (const part of state.message.content) {
    if (part.type !== 'tool-call' || part.toolName !== FILE_VIEW_NAME) continue;
    count +=
      normalizeRunCodeResult(part.result).artifacts?.filter(
        (artifact) => artifact.kind === 'file',
      ).length ?? 0;
  }
  return count;
}
