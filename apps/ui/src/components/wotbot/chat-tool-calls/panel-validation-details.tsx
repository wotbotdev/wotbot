'use client';

import { useState } from 'react';
import {
  parsePanelValidation,
  validationLabel,
  type PanelValidation,
} from './panel-validation-model';

function Screenshot({
  path,
  viewport,
}: {
  path: string;
  viewport: 'normal' | 'narrow';
}) {
  const [failed, setFailed] = useState(false);
  const url = `${path}/screenshot?viewport=${viewport}`;
  return (
    <div className="space-y-1">
      <p>
        {viewport === 'normal'
          ? 'Normal width · 1000px'
          : 'Narrow width · 390px'}
      </p>
      {failed ? (
        <p role="status">Screenshot unavailable or expired.</p>
      ) : (
        <a
          href={url}
          target="_blank"
          rel="noreferrer"
          aria-label={`Open ${viewport} validation screenshot`}
        >
          {/* Authenticated evidence uses the same-origin proxy without image optimization. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            className={`h-auto w-full rounded border border-border/55 ${viewport === 'narrow' ? 'max-w-[390px]' : ''}`}
            src={url}
            alt={`Panel during validation at ${viewport} width`}
            onError={() => setFailed(true)}
          />
        </a>
      )}
    </div>
  );
}

function VisualReview({
  review,
}: {
  review: NonNullable<PanelValidation['visualReview']>;
}) {
  const labels = {
    clear: 'No visual defects detected',
    warnings: 'Visual warnings',
    unavailable: 'Visual review unavailable',
    disabled: 'Visual review disabled',
  };
  return (
    <div className="space-y-1 rounded bg-muted/40 p-2">
      <p className="font-medium">{labels[review.status]} · Advisory</p>
      {review.reason ? <p>{review.reason}</p> : null}
      {review.findings.length ? (
        <ul className="space-y-1">
          {review.findings.map((finding, index) => (
            <li key={index}>
              {finding.viewport === 'narrow' ? 'Narrow' : 'Normal'} width
              {finding.verdict === 'uncertain' ? ' (uncertain)' : ''}:{' '}
              {finding.evidence}
            </li>
          ))}
        </ul>
      ) : null}
      {['clear', 'warnings'].includes(review.status) ? (
        <p className="text-muted-foreground">
          Reviewed visible content, map tiles, error messages, clipping and
          overlap at two widths. Findings do not block delivery or verify the
          data.
          {review.latencyMs !== undefined
            ? ` Review took ${(review.latencyMs / 1000).toFixed(1)}s.`
            : ''}
        </p>
      ) : null}
    </div>
  );
}

function AttemptDetails({ validation }: { validation: PanelValidation }) {
  const [showScreenshot, setShowScreenshot] = useState(false);
  const path = validation.reportId
    ? `/api/panel-validation/${validation.reportId}`
    : undefined;
  return (
    <section className="space-y-2 rounded-md border border-border/55 p-3">
      <p className="font-medium">
        {validation.attempt && validation.status !== 'blocked'
          ? `Attempt ${validation.attempt}: `
          : ''}
        {validationLabel(validation)}
      </p>
      {validation.visualReview ? (
        <VisualReview review={validation.visualReview} />
      ) : null}
      {validation.diagnostics.map((item, index) => (
        <p key={index} className="break-words text-muted-foreground">
          {item.message}
        </p>
      ))}
      {validation.checks.length > 0 ? (
        <ul className="space-y-1">
          {validation.checks.map((check, index) => (
            <li key={index} className="break-words">
              <span
                className={
                  check.passed ? 'text-muted-foreground' : 'text-destructive'
                }
              >
                {check.passed ? 'Passed' : 'Failed'}:{' '}
              </span>
              {check.label}
              {check.error ? ` — ${check.error}` : ''}
            </li>
          ))}
        </ul>
      ) : null}
      {validation.status === 'failed' &&
      validation.repairsRemaining !== undefined ? (
        <p className="text-muted-foreground">
          {validation.retryAllowed
            ? `${validation.repairsRemaining} repair attempt(s) remaining.`
            : 'No automatic repairs remaining.'}
        </p>
      ) : null}
      {path ? (
        <div className="flex flex-wrap gap-3">
          <a
            className="underline underline-offset-2"
            href={path}
            target="_blank"
            rel="noreferrer"
          >
            Open saved report
          </a>
          {validation.hasScreenshot ? (
            <button
              type="button"
              className="underline underline-offset-2"
              onClick={() => setShowScreenshot(!showScreenshot)}
            >
              {showScreenshot ? 'Hide screenshots' : 'Show screenshots'}
            </button>
          ) : (
            <span className="text-muted-foreground">
              No screenshot captured
            </span>
          )}
        </div>
      ) : null}
      {showScreenshot && path ? (
        <div className="space-y-3">
          <Screenshot path={path} viewport="normal" />
          {validation.hasNarrowScreenshot ? (
            <Screenshot path={path} viewport="narrow" />
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

export function PanelValidationDetails({
  validation,
}: {
  validation: PanelValidation;
}) {
  const [history, setHistory] = useState<PanelValidation[]>([]);
  const [historyState, setHistoryState] = useState<
    'idle' | 'loading' | 'loaded' | 'error'
  >('idle');
  async function loadHistory() {
    if (historyState !== 'idle' || !validation.previousReports.length) return;
    setHistoryState('loading');
    const results = await Promise.allSettled(
      validation.previousReports.map(async (id) => {
        const response = await fetch(`/api/panel-validation/${id}`, {
          cache: 'no-store',
        });
        if (!response.ok) throw new Error('Report unavailable');
        const parsed = parsePanelValidation(await response.json());
        if (!parsed) throw new Error('Invalid report');
        return parsed;
      }),
    );
    setHistory(
      results.flatMap((result) =>
        result.status === 'fulfilled' ? [result.value] : [],
      ),
    );
    setHistoryState(
      results.some((result) => result.status === 'rejected')
        ? 'error'
        : 'loaded',
    );
  }
  return (
    <details
      className="text-xs"
      onToggle={(event) => {
        if (event.currentTarget.open) void loadHistory();
      }}
    >
      <summary className="cursor-pointer py-1 text-muted-foreground">
        {validationLabel(validation)}
        {validation.visualReview?.status === 'warnings'
          ? ' · Visual warnings'
          : ''}
        {validation.visualReview?.status === 'unavailable'
          ? ' · Visual review unavailable'
          : ''}
        {validation.status === 'passed' && (validation.attempt ?? 1) > 1
          ? ` after ${(validation.attempt ?? 1) - 1} repair(s)`
          : ''}{' '}
        · View validation
      </summary>
      <div className="space-y-2 py-2">
        {historyState === 'loading' ? (
          <p role="status">Loading previous attempts…</p>
        ) : null}
        {history.map((attempt) => (
          <AttemptDetails key={attempt.reportId} validation={attempt} />
        ))}
        {historyState === 'error' ? (
          <p role="status">Some earlier reports are unavailable or expired.</p>
        ) : null}
        <AttemptDetails validation={validation} />
        <p className="text-muted-foreground">
          Checks cover initialization and captured viewports. Buttons, writes,
          actions, subscriptions and factual correctness are untested.
          {!validation.visualReview
            ? ' These screenshots were not reviewed automatically.'
            : ''}
        </p>
        {validation.expiresAt ? (
          <p className="text-muted-foreground">
            Saved evidence expires {validation.expiresAt.slice(0, 10)} (UTC).
          </p>
        ) : null}
      </div>
    </details>
  );
}
