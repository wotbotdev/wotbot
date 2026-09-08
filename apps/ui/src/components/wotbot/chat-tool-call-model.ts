import {
  parseWotInteractionList,
  type WotInteraction,
} from '@/lib/wot-interactions';

export type ToolCallStatus = 'inProgress' | 'executing' | 'complete';

export type CatchAllToolCallRenderProps = {
  args: unknown;
  name: string;
  result: unknown;
  status: ToolCallStatus;
};

export type RunCodeArtifact = {
  filename: string;
  kind: 'image' | 'plotly' | 'file';
  ref: string;
  id?: string;
  mime_type?: string;
  size_bytes?: number;
  sha256?: string;
  expires_at?: string;
};

export type RunCodeResult = {
  artifacts?: RunCodeArtifact[];
  stdout?: string;
  error?: string;
  wotInteractions?: WotInteraction[];
};

export function artifactKey(artifact: {
  kind: string;
  filename: string;
  id?: string;
}): string {
  return `${artifact.kind}:${artifact.id ?? artifact.filename}`;
}

export const TOOL_STATUS_DESCRIPTION: Record<ToolCallStatus, string> = {
  inProgress: 'Collecting the tool inputs and preparing the next step.',
  executing: 'Running the tool with the collected inputs.',
  complete: 'Finished and returned structured data to the assistant.',
};

export function hasInspectableData(value: unknown) {
  if (value === undefined || value === null) {
    return false;
  }

  if (typeof value === 'string') {
    return value.trim().length > 0;
  }

  if (Array.isArray(value)) {
    return value.length > 0;
  }

  if (typeof value === 'object') {
    return Object.keys(value as Record<string, unknown>).length > 0;
  }

  return true;
}

export function formatToolName(name: string) {
  return name
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

export function formatToolData(value: unknown) {
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (!trimmed) {
      return '';
    }

    try {
      return JSON.stringify(JSON.parse(trimmed), null, 2);
    } catch {
      return value;
    }
  }

  if (typeof value === 'object') {
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }

  return String(value);
}

export function summarizeValue(label: string, value: unknown) {
  if (!hasInspectableData(value)) {
    return `${label}: none`;
  }

  if (typeof value === 'string') {
    return `${label}: text`;
  }

  if (Array.isArray(value)) {
    const count = value.length;
    return `${label}: ${count} item${count === 1 ? '' : 's'}`;
  }

  if (typeof value === 'object') {
    const count = Object.keys(value as Record<string, unknown>).length;
    return `${label}: ${count} field${count === 1 ? '' : 's'}`;
  }

  return `${label}: ready`;
}

export function hasErrorResult(value: unknown) {
  if (!value) {
    return false;
  }

  if (typeof value === 'string') {
    return /^error\b/i.test(value.trim());
  }

  if (typeof value === 'object' && !Array.isArray(value)) {
    const error = (value as { error?: unknown }).error;
    return typeof error === 'string' && error.trim().length > 0;
  }

  return false;
}

export function formatArtifactSummary(artifacts: RunCodeArtifact[]) {
  const images = artifacts.filter(
    (artifact) => artifact.kind === 'image',
  ).length;
  const charts = artifacts.filter(
    (artifact) => artifact.kind === 'plotly',
  ).length;
  const parts: string[] = [];
  const files = artifacts.filter((artifact) => artifact.kind === 'file').length;

  if (charts) {
    parts.push(`${charts} chart${charts === 1 ? '' : 's'}`);
  }

  if (images) {
    parts.push(`${images} image${images === 1 ? '' : 's'}`);
  }
  if (files) {
    parts.push(`${files} file${files === 1 ? '' : 's'}`);
  }

  return parts.join(' • ');
}

export function formatWotInteractionSummary(interactions: WotInteraction[]) {
  const count = interactions.length;
  return `${count} device interaction${count === 1 ? '' : 's'}`;
}

export function normalizeRunCodeResult(value: unknown): RunCodeResult {
  let rawResult = value;

  if (typeof rawResult === 'string') {
    try {
      rawResult = JSON.parse(rawResult);
    } catch {
      rawResult = { stdout: rawResult };
    }
  }

  if (!rawResult || typeof rawResult !== 'object' || Array.isArray(rawResult)) {
    return {};
  }

  const raw = rawResult as Record<string, unknown>;
  const artifacts: RunCodeArtifact[] = [];

  if (Array.isArray(raw.artifacts)) {
    for (const artifact of raw.artifacts) {
      if (
        !artifact ||
        typeof artifact !== 'object' ||
        Array.isArray(artifact)
      ) {
        continue;
      }

      const candidate = artifact as Record<string, unknown>;
      const ref = typeof candidate.ref === 'string' ? candidate.ref : '';
      const kind =
        candidate.kind === 'image' ||
        candidate.kind === 'plotly' ||
        candidate.kind === 'file'
          ? candidate.kind
          : null;
      const filename =
        typeof candidate.filename === 'string' ? candidate.filename : '';

      if (ref && kind && filename) {
        if (kind === 'file') {
          const file = normalizeFileArtifact(candidate, ref);
          if (file) artifacts.push(file);
        } else {
          artifacts.push({ ref, kind, filename });
        }
      }
    }
  }

  if (!artifacts.length) {
    const imageFilenames = Array.isArray(raw.images) ? raw.images : [];
    const plotlyFilenames = Array.isArray(raw.plotly) ? raw.plotly : [];

    for (const [index, filename] of imageFilenames.entries()) {
      if (typeof filename === 'string') {
        artifacts.push({
          ref: `image_${index + 1}`,
          kind: 'image',
          filename,
        });
      }
    }

    for (const [index, filename] of plotlyFilenames.entries()) {
      if (typeof filename === 'string') {
        artifacts.push({
          ref: `chart_${index + 1}`,
          kind: 'plotly',
          filename,
        });
      }
    }
  }

  if (Array.isArray(raw.files)) {
    for (const [index, candidate] of raw.files.entries()) {
      if (
        !candidate ||
        typeof candidate !== 'object' ||
        Array.isArray(candidate)
      )
        continue;
      const file = normalizeFileArtifact(candidate, `file_${index + 1}`);
      if (file && !artifacts.some((artifact) => artifact.id === file.id))
        artifacts.push(file);
    }
  }

  return {
    artifacts,
    error: typeof raw.error === 'string' ? raw.error : undefined,
    stdout: typeof raw.stdout === 'string' ? raw.stdout : undefined,
    wotInteractions: parseWotInteractionList(raw.wot_calls),
  };
}

function normalizeFileArtifact(
  candidate: Record<string, unknown>,
  ref: string,
): RunCodeArtifact | null {
  const id = typeof candidate.id === 'string' ? candidate.id : '';
  const filename =
    typeof candidate.filename === 'string' ? candidate.filename : '';
  if (
    !filename ||
    !id.startsWith('file-') ||
    !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$/.test(id) ||
    id.includes('..')
  )
    return null;
  return {
    ref,
    kind: 'file',
    filename,
    id,
    mime_type:
      typeof candidate.mime_type === 'string' ? candidate.mime_type : undefined,
    size_bytes:
      typeof candidate.size_bytes === 'number' &&
      Number.isFinite(candidate.size_bytes) &&
      candidate.size_bytes >= 0
        ? candidate.size_bytes
        : undefined,
    sha256: typeof candidate.sha256 === 'string' ? candidate.sha256 : undefined,
    expires_at:
      typeof candidate.expires_at === 'string'
        ? candidate.expires_at
        : undefined,
  };
}
