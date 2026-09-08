import assert from 'node:assert/strict';
import test from 'node:test';
import { Readable } from 'node:stream';

import { Content } from '@node-wot/core';
import { InteractionOutput } from '@node-wot/core/dist/interaction-output.js';

import { encodeInteractionOutputPayload, rejectHtmlPayload } from './payloads.js';

test('schema-less interaction output falls back to its raw response bytes', async () => {
  const body = Buffer.from('<gml:FeatureCollection/>');
  let arrayBufferReads = 0;
  let valueReads = 0;
  const result = await encodeInteractionOutputPayload({
    form: {
      href: 'wotbot+provider://runtime/things/example/actions/download_gml',
      response: { contentType: 'application/gml+xml' },
    },
    schema: undefined,
    value: async () => {
      valueReads += 1;
      return undefined;
    },
    arrayBuffer: async () => {
      arrayBufferReads += 1;
      return body;
    },
  });

  assert.equal(arrayBufferReads, 1);
  assert.equal(valueReads, 0);
  assert.deepEqual(result.body, body);
  assert.equal(result.contentType, 'application/gml+xml');
  assert.equal(result.sourceProtocol, 'wotbot+provider');
});

test('schema-less action with an empty response remains empty', async () => {
  let arrayBufferReads = 0;
  const result = await encodeInteractionOutputPayload({
    form: {
      href: 'https://device.example/actions/restart',
      response: { contentType: 'application/octet-stream' },
    },
    schema: undefined,
    value: async () => undefined,
    arrayBuffer: async () => {
      arrayBufferReads += 1;
      return new ArrayBuffer(0);
    },
  });

  assert.equal(arrayBufferReads, 1);
  assert.equal(result.body.length, 0);
  assert.equal(result.contentType, 'application/octet-stream');
  assert.equal(result.sourceProtocol, 'https');
});

test('JSON HTML pages fail with and without a schema, including schema-validation fallback', async () => {
  for (const schema of [undefined, { type: 'object' }, { type: 'string' }]) {
    const output = new InteractionOutput(
      new Content('application/json', Readable.from(['<h3>Dataset not found</h3>'])),
      { href: 'https://api.example/data', contentType: 'application/json' },
      schema as any,
    );
    await assert.rejects(encodeInteractionOutputPayload(output), {
      code: 'invalid_response',
      status: 502,
    });
  }
});

test('valid JSON strings containing HTML remain JSON through schema validation and caching', async () => {
  const markup = '<html><body>This is a string value</body></html>';
  for (const schema of [undefined, { type: 'string' }, { type: 'object' }]) {
    const output = new InteractionOutput(
      new Content('application/json', Readable.from([JSON.stringify(markup)])),
      { href: 'https://api.example/data', contentType: 'application/json' },
      schema as any,
    );
    const payload = await encodeInteractionOutputPayload(output);
    assert.equal(JSON.parse(payload.body.toString()), markup);
    assert.doesNotThrow(() => rejectHtmlPayload(payload.body, payload.contentType));
  }
});

test('a parsing failure cannot turn an HTML page into a successful raw fallback', async () => {
  await assert.rejects(
    encodeInteractionOutputPayload({
      form: { href: 'https://api.example/data', contentType: 'application/json' },
      schema: { type: 'object' },
      value: async () => {
        throw new SyntaxError('Invalid JSON');
      },
      arrayBuffer: async () => Buffer.from('<!DOCTYPE html><html>Error</html>'),
    }),
    { code: 'invalid_response', status: 502 },
  );
});

test('tabular error pages fail while legitimate text, XML, HTML, binary and CSV content pass', async () => {
  const page = Buffer.from('\ufeff  <!DOCTYPE HTML><html><body>Error</body></html>');
  for (const contentType of [
    'text/csv',
    'application/csv',
    'text/tab-separated-values',
    'APPLICATION/LD+JSON; charset=utf-8',
  ]) {
    assert.throws(() => rejectHtmlPayload(page, contentType), { code: 'invalid_response' });
  }
  for (const [contentType, body] of [
    ['text/html', page],
    ['text/plain', page],
    ['application/xhtml+xml', page],
    ['application/octet-stream', page],
    ['application/gml+xml', Buffer.from('<gml:FeatureCollection/>')],
    ['text/csv', Buffer.from('name,value\n"<div>markup</div>",1')],
    ['text/csv', Buffer.from('<div>markup</div>,1')],
    ['application/json', Buffer.from('{"status":"credential_required"}')],
  ] as const) {
    const payload = await encodeInteractionOutputPayload({
      form: { contentType },
      arrayBuffer: async () => body,
    });
    assert.deepEqual(payload.body, body);
  }
});

test('non-HTML schema mismatches retain the decoded value and warning', async () => {
  const output = new InteractionOutput(
    new Content('application/json', Readable.from(['42'])),
    { href: 'https://api.example/data', contentType: 'application/json' },
    { type: 'object' },
  );
  let warning: unknown;
  const payload = await encodeInteractionOutputPayload(output, {
    onInvalidSchema: (value) => {
      warning = value;
    },
  });
  assert.equal(warning, 42);
  assert.equal(JSON.parse(payload.body.toString()), 42);
});
