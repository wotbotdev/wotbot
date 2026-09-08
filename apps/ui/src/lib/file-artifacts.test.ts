import assert from 'node:assert/strict';
import test from 'node:test';

import {
  normalizeRunCodeResult,
  formatArtifactSummary,
} from '../components/wotbot/chat-tool-call-model';
import { latestTurnArtifacts } from '../components/wotbot/assistant/artifacts';
import { normalizeJobCodeResult } from './job-code-result';
import { GET } from '../app/api/artifacts/[id]/route';

const file = {
  id: 'file-first.csv',
  kind: 'file' as const,
  ref: 'file_1',
  filename: 'data.csv',
  mime_type: 'text/csv',
  size_bytes: 10,
  sha256: 'digest',
  expires_at: '2026-09-15T12:00:00Z',
};

test('chat and job results preserve downloadable file metadata', () => {
  const result = { artifacts: [file] };
  assert.deepEqual(normalizeRunCodeResult(JSON.stringify(result)).artifacts, [
    file,
  ]);
  assert.deepEqual(normalizeJobCodeResult({ response: result }).artifacts, [
    file,
  ]);
  assert.equal(formatArtifactSummary([file]), '1 file');
  assert.deepEqual(
    normalizeJobCodeResult({
      response: { files: [file], images: ['chart.png'] },
    }).artifacts,
    [{ kind: 'image', ref: 'image_1', filename: 'chart.png' }, file],
  );
});

test('live mode keeps distinct exports with the same display filename', () => {
  const files = [file, { ...file, id: 'file-second.csv', ref: 'file_2' }];
  assert.deepEqual(
    latestTurnArtifacts([
      { type: 'human', content: 'Export both datasets' },
      {
        type: 'tool',
        content: JSON.stringify({ artifacts: [...files, file] }),
      },
    ]),
    files,
  );
});

test('invalid file identities cannot become download links', () => {
  for (const id of [
    '',
    '../secret',
    'file-a/../secret',
    'file-a..csv',
    `file-${'a'.repeat(196)}`,
    'https://external.test/file',
  ]) {
    assert.deepEqual(
      normalizeRunCodeResult({ artifacts: [{ ...file, id }] }).artifacts,
      [],
    );
  }
});

test('artifact proxy streams original bytes and preserves attachment headers', async (t) => {
  const bytes = new Uint8Array([0, 255, 1, 13, 10]);
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(bytes);
      controller.close();
    },
  });
  const upstream = new Response(stream, {
    headers: {
      'content-type': 'application/octet-stream',
      'content-disposition': 'attachment; filename="data.bin"',
      'x-artifact-sha256': 'digest',
      'x-artifact-expires-at': file.expires_at,
    },
  });
  // A proxy that buffers the response would invoke this method.
  t.mock.method(upstream, 'arrayBuffer', () => {
    throw new Error('must stream');
  });
  const fetch = t.mock.method(globalThis, 'fetch', async () => upstream);
  const response = await GET(
    new Request('http://ui.test/api/artifacts/file-first.csv'),
    { params: Promise.resolve({ id: file.id }) },
  );
  assert.equal(
    response.headers.get('content-disposition'),
    'attachment; filename="data.bin"',
  );
  assert.equal(response.headers.get('x-artifact-sha256'), 'digest');
  assert.equal(response.headers.get('x-artifact-expires-at'), file.expires_at);
  assert.match(response.headers.get('cache-control') ?? '', /no-store/);
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()), bytes);
  assert.equal(fetch.mock.callCount(), 1);
});

test('artifact proxy rejects traversal before fetching and explains expired files', async (t) => {
  const fetch = t.mock.method(
    globalThis,
    'fetch',
    async () => new Response('missing', { status: 404 }),
  );
  const request = new Request('http://ui.test/api/artifacts/file-first.csv');
  const invalid = await GET(request, { params: Promise.resolve({ id: '..' }) });
  assert.equal(invalid.status, 400);
  assert.equal(fetch.mock.callCount(), 0);
  const missing = await GET(request, {
    params: Promise.resolve({ id: file.id }),
  });
  assert.equal(missing.status, 404);
  assert.match(await missing.text(), /expired/);
});
