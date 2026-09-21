import assert from 'node:assert/strict';
import test from 'node:test';

import {
  enrichArtifactForPinning,
  isInteractionAllowed,
  normalizeWebInterfaceResult,
} from './web-interface-model';

test('normalizeWebInterfaceResult parses a stringified web artifact', () => {
  const parsed = normalizeWebInterfaceResult(
    JSON.stringify({
      artifacts: [
        {
          ref: 'ui_1',
          kind: 'web',
          filename: 'abc123.html',
          capabilities: [
            {
              thingId: 'urn:wotbot:thing:packaging-conveyor',
              affordances: ['speed', 'running'],
              ops: ['writeProperty', 'observeProperty', 'bogusOp'],
            },
          ],
        },
      ],
    }),
  );

  assert.deepEqual(parsed.artifact, {
    ref: 'ui_1',
    filename: 'abc123.html',
    capabilities: [
      {
        thingId: 'urn:wotbot:thing:packaging-conveyor',
        affordances: ['speed', 'running'],
        ops: ['writeProperty', 'observeProperty'],
      },
    ],
  });
});

test('normalizeWebInterfaceResult ignores non-web artifacts and surfaces errors', () => {
  assert.deepEqual(
    normalizeWebInterfaceResult({
      artifacts: [{ ref: 'chart_1', kind: 'plotly', filename: 'x.json' }],
    }).artifact,
    undefined,
  );

  assert.equal(
    normalizeWebInterfaceResult({ error: 'no capabilities' }).error,
    'no capabilities',
  );
});

test('normalizeWebInterfaceResult drops capabilities without a thing or ops', () => {
  const parsed = normalizeWebInterfaceResult({
    artifacts: [
      {
        ref: 'ui_1',
        kind: 'web',
        filename: 'a.html',
        capabilities: [
          { thingId: '', ops: ['readProperty'] },
          { thingId: 't1', ops: [] },
          { thingId: 't2', affordances: ['x'], ops: ['readProperty'] },
        ],
      },
    ],
  });

  assert.deepEqual(parsed.artifact?.capabilities, [
    { thingId: 't2', affordances: ['x'], ops: ['readProperty'] },
  ]);
});

test('enrichArtifactForPinning merges html and title from tool args', () => {
  const artifact = {
    ref: 'ui_1',
    filename: 'a.html',
    capabilities: [],
  };
  const enriched = enrichArtifactForPinning(artifact, {
    html: '<div>hi</div>',
    title: 'My panel',
  });
  assert.equal(enriched.html, '<div>hi</div>');
  assert.equal(enriched.title, 'My panel');

  // Missing/invalid args leave html undefined.
  assert.equal(enrichArtifactForPinning(artifact, undefined).html, undefined);
});

test('data-only panels retain resolved attachment references for pinning', () => {
  const parsed = normalizeWebInterfaceResult({
    artifacts: [
      {
        ref: 'ui_1',
        kind: 'web',
        filename: 'map.html',
        capabilities: [],
        data: { areas: 'panel-data-snapshot', invalid: 42 },
      },
    ],
  });
  assert.ok(parsed.artifact);
  const pinned = enrichArtifactForPinning(parsed.artifact, {
    html: '<div id="map"></div>',
    title: 'Map',
    data: { areas: 'file-original.geojson' },
  });
  assert.deepEqual(pinned.data, { areas: 'panel-data-snapshot' });
  assert.deepEqual(pinned.capabilities, []);
  assert.equal(pinned.html, '<div id="map"></div>');
});

const caps = [
  {
    thingId: 'conveyor',
    affordances: ['speed'],
    ops: ['writeProperty' as const],
  },
  { thingId: 'scanner', affordances: [], ops: ['readProperty' as const] },
];

test('validation evidence is retained on failures and on delivered artifacts', () => {
  const validation = {
    status: 'passed',
    report_id: 'a'.repeat(32),
    previous_reports: ['b'.repeat(32)],
    attempt: 2,
    repairs_remaining: 1,
    retry_allowed: false,
    has_screenshot: true,
    has_narrow_screenshot: true,
    visual_review: {
      status: 'warnings',
      mode: 'advisory',
      latency_ms: 1234,
      assessments: [
        {
          viewport: 'normal',
          category: 'overlap',
          verdict: 'absent',
          evidence: '',
        },
        {
          viewport: 'narrow',
          category: 'clipped_text',
          verdict: 'present',
          evidence: 'Legend is clipped at the right edge.',
        },
      ],
    },
    checks: { checks: [{ label: 'map rendered', passed: true, error: null }] },
    diagnostics: [],
    untested_operations: ['user_interactions'],
  };
  const parsed = normalizeWebInterfaceResult({
    browser_validation: validation,
    artifacts: [{ kind: 'web', ref: 'ui_1', filename: 'map.html' }],
  });
  assert.equal(parsed.artifact?.validation?.reportId, 'a'.repeat(32));
  assert.equal(parsed.artifact?.validation?.attempt, 2);
  assert.deepEqual(parsed.artifact?.validation?.previousReports, [
    'b'.repeat(32),
  ]);
  assert.equal(parsed.artifact?.validation?.hasScreenshot, true);
  assert.equal(parsed.artifact?.validation?.hasNarrowScreenshot, true);
  assert.equal(parsed.artifact?.validation?.status, 'passed');
  assert.equal(parsed.artifact?.validation?.visualReview?.status, 'warnings');
  assert.equal(parsed.artifact?.validation?.visualReview?.findings.length, 1);
  assert.equal(
    parsed.artifact?.validation?.visualReview?.findings[0].viewport,
    'narrow',
  );
  const failed = normalizeWebInterfaceResult({
    error: 'Failed',
    browser_validation: { ...validation, status: 'failed' },
  });
  assert.equal(failed.validation?.status, 'failed');
  assert.equal(failed.artifact, undefined);
});

test('validation rejects untrusted report paths and malformed check payloads', () => {
  const parsed = normalizeWebInterfaceResult({
    browser_validation: {
      status: 'failed',
      report_id: '../secrets',
      previous_reports: ['https://example.com', 'c'.repeat(32)],
      checks: { checks: [null, { label: 'not boolean', passed: 'true' }, {}] },
      diagnostics: [
        null,
        { message: 99 },
        { kind: 'self_check', message: 'Failed' },
      ],
    },
  }).validation;
  assert.equal(parsed?.reportId, undefined);
  assert.deepEqual(parsed?.previousReports, ['c'.repeat(32)]);
  assert.equal(parsed?.checks.length, 1);
  assert.equal(parsed?.checks[0].passed, false);
  assert.equal(parsed?.diagnostics.length, 1);
});

test('isInteractionAllowed enforces thing, op, and affordance', () => {
  // Allowed: exact thing + op + listed affordance.
  assert.equal(
    isInteractionAllowed(caps, 'writeProperty', 'conveyor', 'speed'),
    true,
  );
  // Empty affordance list means any affordance on that thing.
  assert.equal(
    isInteractionAllowed(caps, 'readProperty', 'scanner', 'anything'),
    true,
  );
});

test('isInteractionAllowed rejects out-of-scope interactions', () => {
  // Wrong affordance on a scoped thing.
  assert.equal(
    isInteractionAllowed(caps, 'writeProperty', 'conveyor', 'direction'),
    false,
  );
  // Op not granted.
  assert.equal(
    isInteractionAllowed(caps, 'invokeAction', 'conveyor', 'speed'),
    false,
  );
  // Thing not in the allowlist.
  assert.equal(
    isInteractionAllowed(caps, 'writeProperty', 'robot-arm', 'speed'),
    false,
  );
});
