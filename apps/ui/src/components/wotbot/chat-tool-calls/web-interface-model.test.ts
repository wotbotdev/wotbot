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

const caps = [
  {
    thingId: 'conveyor',
    affordances: ['speed'],
    ops: ['writeProperty' as const],
  },
  { thingId: 'scanner', affordances: [], ops: ['readProperty' as const] },
];

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
