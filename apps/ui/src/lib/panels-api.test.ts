import assert from 'node:assert/strict';
import test from 'node:test';

import { parsePanelValidation } from '../components/wotbot/chat-tool-calls/panel-validation-model';
import { HttpError } from './http-client';
import { editPanel } from './panels-api';

const validation = {
  status: 'passed',
  report_id: 'a'.repeat(32),
  previous_reports: ['b'.repeat(32)],
  has_screenshot: true,
  has_narrow_screenshot: true,
  visual_review: {
    status: 'warnings',
    assessments: [
      {
        viewport: 'narrow',
        category: 'overlap',
        verdict: 'present',
        evidence: 'The legend overlaps a label.',
      },
    ],
  },
};

test('AI panel edits preserve validation warnings and evidence references', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input, init) => {
    assert.equal(input, '/api/panels/panel-a/edit');
    assert.equal(init?.method, 'POST');
    assert.deepEqual(JSON.parse(String(init?.body)), {
      instruction: 'Update the heading',
    });
    return Response.json({ id: 'panel-a', browser_validation: validation });
  }) as typeof fetch;
  try {
    const result = await editPanel('panel-a', 'Update the heading');
    const report = parsePanelValidation(result.browser_validation);
    assert.equal(result.id, 'panel-a');
    assert.equal(
      report?.visualReview?.findings[0].evidence,
      'The legend overlaps a label.',
    );
    assert.equal(report?.hasScreenshot, true);
    assert.deepEqual(report?.previousReports, ['b'.repeat(32)]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('failed AI edits retain diagnostics in the HTTP error for the drawer', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () =>
    Response.json(
      {
        detail: {
          message:
            'Panel edit failed validation. The saved panel was not changed.',
          browser_validation: {
            ...validation,
            status: 'failed',
            diagnostics: [
              { kind: 'javascript', message: 'Chart failed to load' },
            ],
          },
        },
      },
      { status: 422 },
    )) as typeof fetch;
  try {
    await assert.rejects(editPanel('panel-a', 'Update'), (error: unknown) => {
      assert.ok(error instanceof HttpError);
      assert.equal(error.status, 422);
      assert.match(error.message, /saved panel was not changed/);
      const detail = error.detail as { browser_validation: unknown };
      const report = parsePanelValidation(detail.browser_validation);
      assert.equal(report?.status, 'failed');
      assert.equal(report?.diagnostics[0].message, 'Chart failed to load');
      assert.equal(report?.reportId, 'a'.repeat(32));
      return true;
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('ordinary API failures still carry a readable error message', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () =>
    Response.json(
      { detail: 'Agent is not ready' },
      { status: 503 },
    )) as typeof fetch;
  try {
    await assert.rejects(editPanel('panel-a', 'Update'), /Agent is not ready/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
