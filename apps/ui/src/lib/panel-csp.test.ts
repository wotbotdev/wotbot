import assert from 'node:assert/strict';
import test from 'node:test';

import { PANEL_CSP } from './panel-csp';

function directive(name: string): string {
  const found = PANEL_CSP.split('; ').find((part) =>
    part.startsWith(`${name} `),
  );
  assert.ok(found, `expected a ${name} directive`);
  return found;
}

test('nothing loads by default', () => {
  assert.ok(PANEL_CSP.startsWith("default-src 'none'"));
});

test('no directive opens up to any host', () => {
  // The whole containment story rests on a panel never choosing its own host.
  // `*`, bare `https:`, or `data:` for scripts would each undo that.
  for (const part of PANEL_CSP.split('; ')) {
    assert.ok(!/\s\*/.test(part), `wildcard host in: ${part}`);
    assert.ok(!/\shttps:(\s|$)/.test(part), `scheme-wide source in: ${part}`);
  }
  assert.ok(!directive('script-src').includes('data:'));
});

test('script-src still bounds which hosts may serve code', () => {
  // 'unsafe-eval' is allowed deliberately -- the document is generated code and
  // already has 'unsafe-inline'. The host allowlist is the part that matters.
  const scriptSrc = directive('script-src');

  assert.ok(scriptSrc.includes("'unsafe-eval'"));
  for (const source of scriptSrc.replace('script-src ', '').split(' ')) {
    assert.ok(
      source.startsWith("'") || source.startsWith('https://'),
      `unexpected script-src source: ${source}`,
    );
  }
});

test('styles and fonts may come from every host scripts may', () => {
  // A stylesheet from a host that can already serve executable JS grants
  // strictly less, and libraries ship their CSS beside their JS.
  for (const host of [
    'https://cdn.jsdelivr.net',
    'https://unpkg.com',
    'https://cdnjs.cloudflare.com',
  ]) {
    assert.ok(directive('script-src').includes(host), `script-src ${host}`);
    assert.ok(directive('style-src').includes(host), `style-src ${host}`);
    assert.ok(directive('font-src').includes(host), `font-src ${host}`);
  }
});

test('images are restricted to named hosts', () => {
  const imgSrc = directive('img-src');

  assert.ok(imgSrc.includes("'self'"));
  assert.ok(imgSrc.includes('data:'));
  assert.ok(imgSrc.includes('https://*.tile.openstreetmap.org'));
  // An arbitrary image URL is an exfiltration channel, so every source here
  // must be one the panel could not have chosen.
  for (const source of imgSrc.replace('img-src ', '').split(' ')) {
    assert.ok(
      source === "'self'" ||
        source === 'data:' ||
        // Captured camera stills are blob: URLs, which never leave the page.
        source === 'blob:' ||
        source.startsWith('https://'),
      `unexpected img-src source: ${source}`,
    );
  }
});

test('tile hosts are reachable by fetch, not just by <img>', () => {
  // WebGL map renderers (maplibre, and so Plotly's map traces) fetch() their
  // tiles, so an img-src-only allowance renders a blank map.
  const connectSrc = directive('connect-src');

  for (const host of [
    'https://tile.openstreetmap.org',
    'https://*.tile.openstreetmap.org',
  ]) {
    assert.ok(directive('img-src').includes(host), `img-src ${host}`);
    assert.ok(connectSrc.includes(host), `connect-src ${host}`);
  }
  // Still no 'self': panels share the app's server, so 'self' would let a panel
  // call the app's own API.
  assert.ok(!connectSrc.includes("'self'"));
});

test('connect-src reaches in-document bytes, which are not egress', () => {
  // three.js fetches a .glb's embedded buffers and textures back off blob:
  // URLs it created itself; neither scheme can reach a server.
  const connectSrc = directive('connect-src');

  assert.ok(connectSrc.includes('blob:'));
  assert.ok(connectSrc.includes('data:'));
});

test('panels cannot navigate, post, or nest frames', () => {
  assert.ok(PANEL_CSP.includes("form-action 'none'"));
  assert.ok(PANEL_CSP.includes("base-uri 'none'"));
  assert.ok(PANEL_CSP.includes("frame-src 'none'"));
});
