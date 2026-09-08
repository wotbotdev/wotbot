import {
  artifactKey,
  normalizeRunCodeResult,
  type RunCodeArtifact,
} from '@/components/wotbot/chat-tool-call-model';
import {
  enrichArtifactForPinning,
  normalizeWebInterfaceResult,
  type WebInterfaceArtifact,
} from '@/components/wotbot/chat-tool-calls/web-interface-model';
import type { LangChainMessage } from '@/lib/thread-messages';

export type LiveModeWebArtifact = WebInterfaceArtifact & { kind: 'web' };
export type LiveModeArtifact = RunCodeArtifact | LiveModeWebArtifact;

/**
 * Artifacts produced since the last user message.
 *
 * Live mode shows only the current turn's visual artifacts, so this walks back
 * to the most recent human message and collects what the tools returned after it.
 */
export function latestTurnArtifacts(
  messages: readonly LangChainMessage[] | undefined,
): LiveModeArtifact[] {
  if (!messages?.length) {
    return [];
  }

  let lastHumanIndex = -1;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const type = messages[index]?.type;
    if (type === 'human' || type === 'user') {
      lastHumanIndex = index;
      break;
    }
  }
  if (lastHumanIndex < 0) {
    return [];
  }

  const artifacts: LiveModeArtifact[] = [];
  const seen = new Set<string>();
  const webInterfaceArgsByCallId = new Map<string, unknown>();

  for (const message of messages.slice(lastHumanIndex + 1)) {
    for (const call of message.tool_calls ?? []) {
      if (call.id && call.name === 'create_web_interface') {
        webInterfaceArgsByCallId.set(call.id, call.args);
      }
    }

    if (message.type !== 'tool') {
      continue;
    }

    const runCodeResult = normalizeRunCodeResult(message.content);
    for (const artifact of runCodeResult.artifacts ?? []) {
      const key = artifactKey(artifact);
      if (seen.has(key)) {
        continue;
      }
      seen.add(key);
      artifacts.push(artifact);
    }

    const webInterfaceResult = normalizeWebInterfaceResult(message.content);
    if (webInterfaceResult.artifact) {
      const artifact = enrichArtifactForPinning(
        webInterfaceResult.artifact,
        message.tool_call_id
          ? webInterfaceArgsByCallId.get(message.tool_call_id)
          : undefined,
      );
      const key = `web:${artifact.filename}`;
      if (!seen.has(key)) {
        seen.add(key);
        artifacts.push({ ...artifact, kind: 'web' });
      }
    }
  }

  return artifacts;
}
