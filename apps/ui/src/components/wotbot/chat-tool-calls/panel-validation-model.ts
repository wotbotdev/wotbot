export type PanelValidation = {
  status: 'passed' | 'failed' | 'inconclusive' | 'unavailable' | 'blocked';
  reportId?: string;
  previousReports: string[];
  hasScreenshot: boolean;
  hasNarrowScreenshot: boolean;
  visualReview?: {
    status: 'clear' | 'warnings' | 'unavailable' | 'disabled';
    reason?: string;
    latencyMs?: number;
    findings: {
      viewport: string;
      verdict: string;
      evidence: string;
    }[];
  };
  attempt?: number;
  repairsRemaining?: number;
  retryAllowed: boolean;
  diagnostics: { kind: string; message: string }[];
  checks: { label: string; passed: boolean; error?: string }[];
  expiresAt?: string;
};

const reportId = (value: unknown): value is string =>
  typeof value === 'string' && /^[a-f0-9]{32}$/.test(value);

export function parsePanelValidation(
  value: unknown,
): PanelValidation | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return;
  const raw = value as Record<string, unknown>;
  if (
    !['passed', 'failed', 'inconclusive', 'unavailable', 'blocked'].includes(
      String(raw.status),
    )
  )
    return;
  const records = (value: unknown) =>
    Array.isArray(value)
      ? value.filter(
          (item): item is Record<string, unknown> =>
            !!item && typeof item === 'object' && !Array.isArray(item),
        )
      : [];
  const checks =
    raw.checks && typeof raw.checks === 'object'
      ? (raw.checks as Record<string, unknown>).checks
      : [];
  const visual =
    raw.visual_review && typeof raw.visual_review === 'object'
      ? (raw.visual_review as Record<string, unknown>)
      : undefined;
  const visualReview: PanelValidation['visualReview'] =
    visual &&
    ['clear', 'warnings', 'unavailable', 'disabled'].includes(
      String(visual.status),
    )
      ? {
          status: visual.status as NonNullable<
            PanelValidation['visualReview']
          >['status'],
          reason: typeof visual.reason === 'string' ? visual.reason : undefined,
          latencyMs:
            typeof visual.latency_ms === 'number'
              ? visual.latency_ms
              : undefined,
          findings: records(visual.assessments)
            .filter(
              (item) =>
                ['present', 'uncertain'].includes(String(item.verdict)) &&
                ['normal', 'narrow'].includes(String(item.viewport)) &&
                typeof item.evidence === 'string',
            )
            .map((item) => ({
              viewport: String(item.viewport),
              verdict: String(item.verdict),
              evidence: String(item.evidence),
            })),
        }
      : undefined;
  return {
    status: raw.status as PanelValidation['status'],
    reportId: reportId(raw.report_id) ? raw.report_id : undefined,
    previousReports: Array.isArray(raw.previous_reports)
      ? raw.previous_reports.filter(reportId).slice(-2)
      : [],
    hasScreenshot: raw.has_screenshot === true,
    hasNarrowScreenshot: raw.has_narrow_screenshot === true,
    visualReview,
    attempt: typeof raw.attempt === 'number' ? raw.attempt : undefined,
    repairsRemaining:
      typeof raw.repairs_remaining === 'number'
        ? raw.repairs_remaining
        : undefined,
    retryAllowed: raw.retry_allowed === true,
    diagnostics: records(raw.diagnostics)
      .filter((item) => typeof item.message === 'string')
      .map((item) => ({
        kind: typeof item.kind === 'string' ? item.kind : 'validation',
        message: String(item.message),
      })),
    checks: records(checks)
      .filter((item) => typeof item.label === 'string')
      .map((item) => ({
        label: String(item.label),
        passed: item.passed === true,
        error: typeof item.error === 'string' ? item.error : undefined,
      })),
    expiresAt: typeof raw.expires_at === 'string' ? raw.expires_at : undefined,
  };
}

export function validationLabel(validation: PanelValidation): string {
  const labels = {
    passed: 'Browser check passed',
    failed: 'Panel validation failed',
    inconclusive: 'Panel validation inconclusive',
    unavailable: 'Validation unavailable',
    blocked: 'Automatic retries stopped',
  };
  return labels[validation.status];
}
