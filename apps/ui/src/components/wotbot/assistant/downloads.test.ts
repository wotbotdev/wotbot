import assert from 'node:assert/strict';
import test from 'node:test';

import { downloadFileCount } from './downloads';
import { FILE_VIEW_NAME } from '@/lib/thread-messages';

const file = (ref: string) => ({
  kind: 'file',
  ref,
  id: `file-${ref}.json`,
  filename: `${ref}.json`,
});
const state = (
  content: Array<{ type: string; toolName?: string; result?: unknown }>,
) => ({ message: { content } });

test('counts the files of every download part', () => {
  assert.equal(
    downloadFileCount(
      state([
        {
          type: 'tool-call',
          toolName: FILE_VIEW_NAME,
          result: { artifacts: [file('a'), file('b')] },
        },
        { type: 'text' },
        {
          type: 'tool-call',
          toolName: FILE_VIEW_NAME,
          result: { artifacts: [file('c')] },
        },
      ]),
    ),
    3,
  );
});

test('ignores other tool calls and non-file artifacts', () => {
  assert.equal(
    downloadFileCount(
      state([
        {
          type: 'tool-call',
          toolName: 'run_code',
          result: { artifacts: [file('a')] },
        },
        {
          type: 'tool-call',
          toolName: FILE_VIEW_NAME,
          result: { artifacts: [{ kind: 'image', ref: 'plot' }, file('b')] },
        },
      ]),
    ),
    1,
  );
});
