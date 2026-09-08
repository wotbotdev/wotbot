import assert from 'node:assert/strict';
import test from 'node:test';

import { getPanelOrigin, isPanelHostname, toPanelLabel } from './panel-origin';

const local = { protocol: 'http:', hostname: 'localhost', port: '3000' };
const prod = { protocol: 'https:', hostname: 'wotbot.example.com', port: '' };

test('a panel id becomes its own subdomain', () => {
  const origin = getPanelOrigin('39ecb1f12b2146ffb42783f71d5e144a', local, '');
  assert.match(
    origin,
    /^http:\/\/39ecb1f12b2146ffb42783f71d5e144a-[a-z0-9]+\.panels\.localhost:3000$/,
  );
});

test('keys that normalize alike still get separate origins', () => {
  // They would otherwise share an origin, and so share one camera grant.
  assert.notEqual(
    getPanelOrigin('chart_1.html', local, ''),
    getPanelOrigin('chart-1.html', local, ''),
  );
});

test('a template not led by {key} is refused rather than trusted', () => {
  // Its suffix would match the app's own host and 404 the whole application.
  assert.equal(
    isPanelHostname('app.example.com', 'panel-{key}.example.com'),
    false,
  );
});

test('each panel gets a different origin', () => {
  // The point of per-panel origins: a camera grant for one must not arm another.
  assert.notEqual(
    getPanelOrigin('panel-a', local, ''),
    getPanelOrigin('panel-b', local, ''),
  );
});

test('a filename collapses to a single DNS label', () => {
  // A wildcard certificate matches one label, so dots must not survive.
  const origin = getPanelOrigin('wot_interface.abc.html', prod, '');
  const label = origin.replace('https://', '').split('.panels.')[0];

  assert.ok(!label.includes('.'), `label still dotted: ${label}`);
  assert.match(label, /^[a-z0-9-]+$/);
});

test('labels stay within the 63-character DNS limit', () => {
  assert.ok(toPanelLabel('x'.repeat(200)).length <= 63);
  assert.ok(!toPanelLabel(`${'x'.repeat(62)}-.html`).endsWith('-'));
  // Truncation must not reintroduce collisions.
  assert.notEqual(toPanelLabel('x'.repeat(200)), toPanelLabel('x'.repeat(201)));
});

test('an unusable key still yields a valid label', () => {
  assert.match(toPanelLabel('...'), /^panel-[a-z0-9]+$/);
  assert.match(toPanelLabel(''), /^panel-[a-z0-9]+$/);
});

test('a configured template places the key and keeps the port', () => {
  assert.match(
    getPanelOrigin('abc', prod, '{key}.panels.example.com'),
    /^https:\/\/abc-[a-z0-9]+\.panels\.example\.com$/,
  );
  // No app port appended: the template names a host that serves its own.
  assert.match(
    getPanelOrigin('abc', local, '{key}.sandbox.test'),
    /^http:\/\/abc-[a-z0-9]+\.sandbox\.test$/,
  );
});

test('server rendering yields no origin rather than a wrong one', () => {
  assert.equal(getPanelOrigin('abc', undefined, ''), '');
});

test('panel hosts are told apart from the app host', () => {
  assert.equal(isPanelHostname('abc.panels.localhost', ''), true);
  assert.equal(isPanelHostname('localhost', ''), false);
  assert.equal(isPanelHostname('wotbot.example.com', ''), false);
  // The bare suffix is not itself a panel host.
  assert.equal(
    isPanelHostname('panels.example.com', '{key}.panels.example.com'),
    false,
  );
  assert.equal(
    isPanelHostname('abc.panels.example.com', '{key}.panels.example.com'),
    true,
  );
});

test('a template with a port still isolates panel hosts from console routes', () => {
  // A request hostname never carries a port, so a template that does must have
  // it stripped before the suffix match -- otherwise nothing is a panel host
  // and the proxy hands panel traffic to the app.
  const template = '{key}.panels.localhost:3131';
  const origin = getPanelOrigin('saved-panel', local, template);

  assert.equal(new URL(origin).port, '3131');
  assert.equal(isPanelHostname(new URL(origin).hostname, template), true);
  assert.equal(isPanelHostname('localhost', template), false);
  assert.equal(isPanelHostname('panels.localhost', template), false);
});
