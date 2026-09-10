import assert from 'node:assert/strict';
import test from 'node:test';

import { proxyDiscoveryDownload } from './discovery-download';
import {
  applyThingRefresh,
  fetchSources,
  previewThingRefresh,
  registerDetectedSource,
} from './sources-api';
import { enrichThing, fetchThings } from './things-api';

test('fetchThings sends the structured origin filter', async () => {
  const calls: string[] = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input) => {
    calls.push(String(input));
    return Response.json({
      items: [
        {
          id: 'urn:thing:external',
          title: 'External',
          description: '',
          tags: [],
          origin: {
            kind: 'discovery',
            provider: 'toolhive',
            source_id: 'urn:source:toolhive',
            external_id: 'server',
          },
        },
      ],
      total: 1,
    });
  }) as typeof fetch;

  try {
    const result = await fetchThings(1, 12, '', 'discovery');
    assert.equal(
      calls[0],
      '/api/things?page=1&per_page=12&origin_kind=discovery',
    );
    assert.equal(result.data[0]?.origin.provider, 'toolhive');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('source APIs use the dedicated registry routes', async () => {
  const calls: Array<{ input: string; init?: RequestInit }> = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input, init) => {
    calls.push({ input: String(input), init });
    if (String(input).includes('/detect')) {
      return Response.json({
        created: true,
        source: { source_id: 'urn:source', provider: 'toolhive' },
      });
    }
    return Response.json({ items: [], total: 0 });
  }) as typeof fetch;

  try {
    await fetchSources(1, 12, 'weather');
    await registerDetectedSource('http://localhost:8080', 'private');
    assert.equal(
      calls[0]?.input,
      '/api/discovery/sources?page=1&per_page=12&q=weather',
    );
    assert.equal(calls[1]?.input, '/api/discovery/sources/detect');
    assert.equal(
      calls[1]?.init?.body,
      JSON.stringify({
        url: 'http://localhost:8080',
        network_access: 'private',
      }),
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('source registration converts HTTP 428 into a secure source challenge', async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (async () => {
    calls += 1;
    if (calls === 1) {
      return Response.json(
        {
          detail: {
            status: 'credential_required',
            owner_kind: 'source',
            source_id: 'urn:source:edc',
            security_name: 'source_sc',
            scheme: 'apikey',
          },
        },
        { status: 428 },
      );
    }
    return Response.json({
      source_id: 'urn:source:edc',
      provider: 'edc-v3',
      config: {},
    });
  }) as typeof fetch;

  try {
    const result = await registerDetectedSource(
      'https://provider.example',
      'public',
    );
    assert.equal(result.source?.source_id, 'urn:source:edc');
    assert.equal(result.credential_challenge?.scheme, 'apikey');
    assert.equal(calls, 2);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('OpenAPI refresh uses preview then applies the opaque refresh id', async () => {
  const calls: Array<{ input: string; init?: RequestInit }> = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input, init) => {
    calls.push({ input: String(input), init });
    if (String(input).endsWith('/preview')) {
      return Response.json({
        refresh_id: 'opaque-refresh',
        expires_in_seconds: 600,
        thing_id: 'urn:thing:api',
        diff: {
          added_actions: ['createPet'],
          removed_actions: [],
          changed_actions: ['getPet'],
          metadata_changed: false,
          server_changed: false,
          security_changed: false,
        },
        warnings: [],
      });
    }
    return Response.json({ refreshed: true });
  }) as typeof fetch;

  try {
    const preview = await previewThingRefresh('urn:thing:api');
    await applyThingRefresh('urn:thing:api', preview.refresh_id);
    assert.equal(
      calls[0]?.input,
      '/api/discovery/things/urn%3Athing%3Aapi/refresh/preview',
    );
    assert.equal(
      calls[1]?.input,
      '/api/discovery/things/urn%3Athing%3Aapi/refresh',
    );
    assert.equal(
      calls[1]?.init?.body,
      JSON.stringify({ refresh_id: 'opaque-refresh' }),
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('discovery downloads keep the upstream stream and range metadata', async () => {
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('partial-data'));
      controller.close();
    },
  });
  const upstream = new Response(stream, {
    status: 206,
    headers: {
      'Content-Type': 'text/csv',
      'Content-Range': 'bytes 0-11/20',
      'Accept-Ranges': 'bytes',
      'Content-Disposition': 'attachment; filename="data.csv"',
      Authorization: 'must-not-be-forwarded',
    },
  });

  const response = proxyDiscoveryDownload(upstream);

  assert.equal(response.status, 206);
  assert.equal(response.headers.get('content-range'), 'bytes 0-11/20');
  assert.equal(response.headers.get('accept-ranges'), 'bytes');
  assert.equal(response.headers.get('authorization'), null);
  assert.equal(
    response.headers.get('cache-control'),
    'private, no-store, max-age=0',
  );
  assert.equal(await response.text(), 'partial-data');
});

test('enrichThing posts a draft document to the enrichment route', async () => {
  const calls: Array<{ input: string; init?: RequestInit }> = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input, init) => {
    calls.push({ input: String(input), init });
    return new Response(
      JSON.stringify({
        enriched: { id: 'urn:thing:alpha', '@type': 'saref:TemperatureSensor' },
        diff: [
          {
            kind: 'type',
            path: '@type',
            value: 'saref:TemperatureSensor',
            label: 'Thing type',
            rationale: 'The title says this is a temperature sensor.',
          },
        ],
        validation: {
          ok: true,
          attempts: 1,
          shacl_conforms: true,
          shacl_findings: [
            {
              severity: 'http://www.w3.org/ns/shacl#Warning',
              message: 'Advisory finding',
            },
          ],
        },
      }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    );
  }) as typeof fetch;

  try {
    const result = await enrichThing('urn:thing:alpha', {
      id: 'urn:thing:alpha',
    });

    assert.equal(calls[0]?.input, '/api/things/urn%3Athing%3Aalpha/enrich');
    assert.equal(calls[0]?.init?.method, 'POST');
    assert.equal(
      calls[0]?.init?.body,
      JSON.stringify({ document: { id: 'urn:thing:alpha' } }),
    );
    assert.equal(result.enriched['@type'], 'saref:TemperatureSensor');
    assert.equal(
      result.diff[0]?.rationale,
      'The title says this is a temperature sensor.',
    );
    assert.equal(
      result.validation.shacl_findings?.[0]?.message,
      'Advisory finding',
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
