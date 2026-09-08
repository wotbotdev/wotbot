import assert from 'node:assert/strict';
import { createServer, type Server } from 'node:http';
import { after, before, beforeEach, mock, test } from 'node:test';

import axios from 'axios';
import { Redis } from 'ioredis';

import { config } from '../config/env.js';
import { buildCacheKey } from '../services/cache.js';
import { decodePayloadEnvelope, encodePayloadEnvelope } from '../services/payloads.js';
import { closeValkeyClient } from '../services/valkey-client.js';
import { handleInvokeAction, handleReadProperty } from './operations.js';
import { getServient, shutdownWot } from './servient.js';

const originalConfig = { ...config };
const cache = new Map<string, string>();
const received: Array<{ method: string; url: string; body: string }> = [];
let server: Server;
let base: string;
let document: any;

before(async () => {
  server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on('data', (chunk: Buffer) => chunks.push(chunk));
    request.on('end', () => {
      const body = Buffer.concat(chunks).toString();
      received.push({ method: request.method || '', url: request.url || '', body });
      response.setHeader('Content-Type', 'application/json');
      if (request.url === '/error-page') {
        response.end('<h3>Dataset not found</h3>');
      } else if (request.url === '/markup') {
        response.end(JSON.stringify('<html><body>A string value</body></html>'));
      } else if (request.url === '/page') {
        response.setHeader('Content-Type', 'text/html');
        response.end('<html><body>API documentation</body></html>');
      } else {
        response.end(
          JSON.stringify({
            query: Object.fromEntries(new URL(request.url || '/', base).searchParams),
            body: body ? JSON.parse(body) : null,
          }),
        );
      }
    });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address === 'object');
  base = `http://127.0.0.1:${address.port}`;

  // Keep catalog, secrets and cache I/O local to this test; use the real HTTP binding.
  Object.assign(config, { cacheEnabled: true, secretsRefreshIntervalMs: 0 });
  mock.method(axios, 'get', async () => ({ data: structuredClone(document) }));
  mock.method(globalThis, 'fetch', async () => Response.json({}));
  mock.method(Redis.prototype, 'connect', async () => undefined);
  mock.method(Redis.prototype, 'get', async (key: string) => cache.get(key) ?? null);
  mock.method(Redis.prototype, 'set', async (key: string, value: string) => {
    cache.set(key, value);
    return 'OK';
  });
  mock.method(Redis.prototype, 'quit', async () => 'OK');
});

beforeEach(() => {
  cache.clear();
  received.length = 0;
  document = {
    '@context': 'https://www.w3.org/2022/wot/td/v1.1',
    id: 'urn:test:runtime-api',
    title: 'Runtime API',
    security: ['nosec_sc'],
    securityDefinitions: { nosec_sc: { scheme: 'nosec' } },
    uriVariables: { language: { type: 'string' } },
    actions: {
      query: {
        safe: true,
        uriVariables: { city: { type: 'integer' } },
        output: { type: 'object' },
        forms: [
          {
            href: `${base}/query{?city,language}`,
            op: ['invokeaction'],
            'htv:methodName': 'GET',
            contentType: 'application/json',
          },
        ],
      },
    },
  };
});

after(async () => {
  await shutdownWot();
  await getServient()?.shutdown();
  await closeValkeyClient();
  mock.restoreAll();
  Object.assign(config, originalConfig);
  if (server) await new Promise<void>((resolve) => server.close(() => resolve()));
});

async function invoke(input?: unknown, uriVariables?: Record<string, unknown>, formIndex?: number) {
  return handleInvokeAction({
    target: { thingId: document.id, affordanceName: 'query' },
    input: encodePayloadEnvelope(input),
    uriVariables: Object.entries(uriVariables || {}).map(([name, value]) => ({
      name,
      value: encodePayloadEnvelope(value),
    })),
    ...(formIndex === undefined ? {} : { formSelector: { formIndex } }),
  });
}

test('runtime expands promoted GET parameters and separates their cached results', async () => {
  const first = await invoke({ city: 12, language: 'de', unknown: 'ignored' });
  const second = await invoke({ city: 34, language: 'en' });
  const repeat = await invoke(undefined, { city: 12, language: 'de' });
  assert.deepEqual(decodePayloadEnvelope(first.completedResult.payload), {
    query: { city: '12', language: 'de' },
    body: null,
  });
  assert.deepEqual(decodePayloadEnvelope(second.completedResult.payload), {
    query: { city: '34', language: 'en' },
    body: null,
  });
  assert.deepEqual(repeat, first);
  assert.equal(received.length, 2);
  assert.ok(received.every((request) => request.body === '' && request.method === 'GET'));
  assert.equal(cache.size, 2);
});

test('runtime gives explicit URI variables precedence over body fields', async () => {
  const result = await invoke({ city: 12 }, { city: 34 });
  assert.deepEqual(decodePayloadEnvelope(result.completedResult.payload), { query: { city: '34' }, body: null });
});

test('runtime recovers GET parameters after skipping an unsupported binding', async () => {
  document.actions.query.forms.unshift({
    href: 'unsupported://api/query',
    op: ['invokeaction'],
    'htv:methodName': 'POST',
    contentType: 'application/json',
  });
  const result = await invoke({ city: 12 });
  assert.deepEqual(decodePayloadEnvelope(result.completedResult.payload), { query: { city: '12' }, body: null });
  assert.equal(received[0].method, 'GET');
});

test('cached action results remain separate for different forms', async () => {
  document.actions.query.forms.push({
    ...document.actions.query.forms[0],
    href: `${base}/query?variant=alternate{&city,language}`,
  });
  const first = await invoke({ city: 12 });
  const alternate = await invoke({ city: 12 }, undefined, 1);
  const repeat = await invoke(undefined, { city: 12 }, 0);
  assert.deepEqual(decodePayloadEnvelope(alternate.completedResult.payload), {
    query: { variant: 'alternate', city: '12' },
    body: null,
  });
  assert.deepEqual(repeat, first);
  assert.equal(received.length, 2);
  assert.equal(cache.size, 2);
});

test('selecting a POST form preserves its body even when fields also name URI variables', async () => {
  document.actions.query.safe = false;
  document.actions.query.input = { type: 'object', required: ['city'], properties: { city: { type: 'integer' } } };
  document.actions.query.forms.push({
    href: `${base}/query{?city}`,
    op: ['invokeaction'],
    'htv:methodName': 'POST',
    contentType: 'application/json',
  });
  const result = await invoke({ city: 12 }, { city: 34 }, 1);
  assert.deepEqual(decodePayloadEnvelope(result.completedResult.payload), {
    query: { city: '34' },
    body: { city: 12 },
  });
  assert.equal(received[0].method, 'POST');
});

test('runtime rejects mislabelled HTML in actions and properties before caching it', async () => {
  document.actions.query.forms[0].href = `${base}/error-page`;
  await assert.rejects(invoke(), { code: 'invalid_response', status: 502 });
  assert.equal(cache.size, 0);
  document.properties = {
    reading: { type: 'object', forms: [{ href: `${base}/error-page`, contentType: 'application/json' }] },
  };
  await assert.rejects(
    handleReadProperty({
      target: { thingId: document.id, affordanceName: 'reading' },
    }),
    { code: 'invalid_response', status: 502 },
  );
});

test('runtime rejects HTML already present in the response cache', async () => {
  cache.set(
    buildCacheKey(document.id, 'invoke_action', 'query', {}, undefined, 0),
    JSON.stringify({
      contentType: 'application/json',
      payload: Buffer.from('<html>Error</html>').toString('base64'),
      statusCode: 200,
    }),
  );
  await assert.rejects(invoke(), { code: 'invalid_response', status: 502 });
  assert.equal(received.length, 0);
});

test('runtime preserves JSON strings containing HTML on both fresh and cached reads', async () => {
  document.actions.query.forms[0].href = `${base}/markup`;
  document.actions.query.output = { type: 'string' };
  const first = await invoke();
  const repeat = await invoke();
  assert.equal(decodePayloadEnvelope(first.completedResult.payload), '<html><body>A string value</body></html>');
  assert.deepEqual(repeat, first);
  assert.equal(received.length, 1);
});

test('runtime accepts HTML when the Thing declares an HTML response', async () => {
  document.actions.query.forms[0].href = `${base}/page`;
  document.actions.query.forms[0].response = { contentType: 'text/html' };
  delete document.actions.query.output;
  const result = await invoke();
  assert.equal(decodePayloadEnvelope(result.completedResult.payload), '<html><body>API documentation</body></html>');
});
