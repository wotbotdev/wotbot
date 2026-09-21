/**
 * Parsing + capability model for the `create_web_interface` tool result.
 *
 * The tool returns `{ artifacts: [{ ref, kind: 'web', filename, capabilities }] }`
 * (or `{ error }`). The capability allowlist is enforced by the trusted parent
 * (see `use-wot-bridge.ts`) before any runtime call is made on behalf of the
 * sandboxed interface.
 */

import {
  parsePanelValidation,
  type PanelValidation,
} from './panel-validation-model';

export const WOT_BRIDGE_OPS = [
  'readProperty',
  'writeProperty',
  'invokeAction',
  'observeProperty',
  'subscribeEvent',
] as const;

export type WotBridgeOp = (typeof WOT_BRIDGE_OPS)[number];

export type WotCapability = {
  thingId: string;
  affordances: string[];
  ops: WotBridgeOp[];
};

export type WebInterfaceArtifact = {
  ref: string;
  filename: string;
  capabilities: WotCapability[];
  data?: Record<string, string>;
  // Populated from the tool call args (not the result) for pinning; the raw
  // agent body markup and its title. Absent when only the result is available.
  html?: string;
  title?: string;
  sourceThreadId?: string | null;
  validation?: PanelValidation;
};

export type WebInterfaceResult = {
  artifact?: WebInterfaceArtifact;
  error?: string;
  validation?: PanelValidation;
};

function parseCapabilities(value: unknown): WotCapability[] {
  if (!Array.isArray(value)) {
    return [];
  }

  const capabilities: WotCapability[] = [];
  for (const entry of value) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
      continue;
    }
    const candidate = entry as Record<string, unknown>;
    const thingId =
      typeof candidate.thingId === 'string' ? candidate.thingId : '';
    if (!thingId) {
      continue;
    }
    const affordances = Array.isArray(candidate.affordances)
      ? candidate.affordances.filter(
          (name): name is string => typeof name === 'string',
        )
      : [];
    const ops = Array.isArray(candidate.ops)
      ? candidate.ops.filter((op): op is WotBridgeOp =>
          (WOT_BRIDGE_OPS as readonly string[]).includes(op as string),
        )
      : [];
    if (!ops.length) {
      continue;
    }
    capabilities.push({ thingId, affordances, ops });
  }
  return capabilities;
}

export function normalizeWebInterfaceResult(
  value: unknown,
): WebInterfaceResult {
  let rawResult = value;

  if (typeof rawResult === 'string') {
    const text = rawResult;
    try {
      rawResult = JSON.parse(text);
    } catch {
      return { error: text };
    }
  }

  if (!rawResult || typeof rawResult !== 'object' || Array.isArray(rawResult)) {
    return {};
  }

  const raw = rawResult as Record<string, unknown>;
  const error = typeof raw.error === 'string' ? raw.error : undefined;
  const validation = parsePanelValidation(raw.browser_validation);

  let artifact: WebInterfaceArtifact | undefined;
  if (Array.isArray(raw.artifacts)) {
    for (const entry of raw.artifacts) {
      if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
        continue;
      }
      const candidate = entry as Record<string, unknown>;
      if (candidate.kind !== 'web') {
        continue;
      }
      const ref = typeof candidate.ref === 'string' ? candidate.ref : '';
      const filename =
        typeof candidate.filename === 'string' ? candidate.filename : '';
      if (!ref || !filename) {
        continue;
      }
      artifact = {
        ref,
        filename,
        capabilities: parseCapabilities(candidate.capabilities),
      };
      if (validation) artifact.validation = validation;
      if (
        candidate.data &&
        typeof candidate.data === 'object' &&
        !Array.isArray(candidate.data)
      ) {
        const entries = Object.entries(candidate.data).filter(
          (entry): entry is [string, string] => typeof entry[1] === 'string',
        );
        if (entries.length) artifact.data = Object.fromEntries(entries);
      }
      break;
    }
  }

  return { artifact, error, validation };
}

/**
 * Merges the raw `html`/`title` from the tool call args into the parsed artifact
 * so the panel can be pinned (the result only carries ref/filename/capabilities).
 */
export function enrichArtifactForPinning(
  artifact: WebInterfaceArtifact,
  args: unknown,
): WebInterfaceArtifact {
  const a = (args ?? {}) as { html?: unknown; title?: unknown };
  return {
    ...artifact,
    html: typeof a.html === 'string' ? a.html : undefined,
    title: typeof a.title === 'string' && a.title ? a.title : artifact.title,
  };
}

/** True when an op on a given thing/affordance is allowed by the allowlist. */
export function isInteractionAllowed(
  capabilities: WotCapability[],
  op: string,
  thingId: string,
  affordanceName: string,
): boolean {
  return capabilities.some((capability) => {
    if (capability.thingId !== thingId) {
      return false;
    }
    if (!(capability.ops as readonly string[]).includes(op)) {
      return false;
    }
    // An empty affordance list means "any affordance on this thing".
    if (!capability.affordances.length) {
      return true;
    }
    return capability.affordances.includes(affordanceName);
  });
}
