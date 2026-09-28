import assert from 'node:assert/strict';
import test from 'node:test';

import { pendingToolName } from './activity';
import { ARTIFACT_VIEW_NAME } from '@/lib/thread-messages';

const state = (
  content: Array<{ type: string; toolName?: string; result?: unknown }>,
) => ({ message: { content } });

test('names the tool call that has no result yet', () => {
  assert.equal(
    pendingToolName(
      state([
        { type: 'tool-call', toolName: 'things_search', result: { items: [] } },
        { type: 'reasoning' },
        { type: 'tool-call', toolName: 'run_code' },
      ]),
    ),
    'Run Code',
  );
});

test('is empty while only reasoning is running', () => {
  assert.equal(
    pendingToolName(
      state([
        { type: 'tool-call', toolName: 'run_code', result: { ok: true } },
        { type: 'reasoning' },
      ]),
    ),
    '',
  );
});

test('prefers the latest of several pending calls', () => {
  assert.equal(
    pendingToolName(
      state([
        { type: 'tool-call', toolName: 'wot_get_action' },
        { type: 'tool-call', toolName: 'things_search' },
      ]),
    ),
    'Things Search',
  );
});

test('ignores synthesized display parts', () => {
  assert.equal(
    pendingToolName(
      state([{ type: 'tool-call', toolName: ARTIFACT_VIEW_NAME }]),
    ),
    '',
  );
});
