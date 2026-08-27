import assert from 'node:assert/strict';
import test from 'node:test';

import {
  countDeviceChangesForRetry,
  findRetryTarget,
  hasAssistantReloadAction,
  hasAssistantResponseActions,
} from './message-actions';
import { WOT_SUMMARY_NAME } from '@/lib/thread-messages';

const state = (
  content: Array<{ type: string; text?: string }>,
  status = 'complete',
) => ({ message: { content, status: { type: status } } });

test('shows actions for a completed textual response', () => {
  assert.equal(
    hasAssistantResponseActions(state([{ type: 'text', text: 'Done.' }])),
    true,
  );
});

test('hides actions for a turn that produced no text', () => {
  assert.equal(
    hasAssistantResponseActions(state([{ type: 'tool-call' }])),
    false,
  );
});

test('shows actions for an answer that used tools', () => {
  // A turn is one message now, so its tool calls sit alongside its answer.
  assert.equal(
    hasAssistantResponseActions(
      state([
        { type: 'reasoning', text: 'checking' },
        { type: 'tool-call' },
        { type: 'text', text: 'The conveyor is running.' },
      ]),
    ),
    true,
  );
});

test('hides actions for reasoning-only, blank, and running messages', () => {
  assert.equal(
    hasAssistantResponseActions(
      state([{ type: 'reasoning', text: 'internal' }]),
    ),
    false,
  );
  assert.equal(
    hasAssistantResponseActions(state([{ type: 'text', text: '   ' }])),
    false,
  );
  assert.equal(
    hasAssistantResponseActions(
      state([{ type: 'text', text: 'Streaming' }], 'running'),
    ),
    false,
  );
});

test('only shows reload when the runtime supports it', () => {
  const message = state([{ type: 'text', text: 'Done.' }]).message;

  assert.equal(
    hasAssistantReloadAction({
      message,
      thread: { capabilities: { reload: true } },
    }),
    true,
  );
  assert.equal(
    hasAssistantReloadAction({
      message,
      thread: { capabilities: { reload: false } },
    }),
    false,
  );
});

test('retry resolves the user turn behind intervening tool groups', () => {
  assert.deepEqual(
    findRetryTarget(
      [
        {
          id: 'user-1',
          role: 'user',
          content: [{ type: 'text', text: 'What do I own?' }],
        },
        {
          id: 'tools-1',
          role: 'assistant',
          content: [{ type: 'tool-call' }],
        },
        {
          id: 'answer-1',
          role: 'assistant',
          content: [{ type: 'text', text: 'Three things.' }],
        },
      ],
      'tools-1',
    ),
    { sourceId: 'user-1', text: 'What do I own?' },
  );
});

test('retry returns null when its parent is unknown', () => {
  assert.equal(
    findRetryTarget(
      [
        {
          id: 'user-1',
          role: 'user',
          content: [{ type: 'text', text: 'Hello' }],
        },
      ],
      'missing',
    ),
    null,
  );
});

test('retry accepts legacy string user content', () => {
  assert.deepEqual(
    findRetryTarget(
      [{ id: 'user-1', role: 'user', content: '  Try again  ' }],
      'user-1',
    ),
    { sourceId: 'user-1', text: 'Try again' },
  );
});

test('retry counts successful writes and actions across the complete user turn', () => {
  const messages = [
    {
      id: 'user-1',
      role: 'user',
      content: [{ type: 'text', text: 'Configure the production line' }],
    },
    {
      id: 'tools-1',
      role: 'assistant',
      content: [{ type: 'tool-call' }],
    },
    {
      id: 'answer-1',
      role: 'assistant',
      content: [{ type: 'text', text: 'Done.' }],
    },
    {
      id: 'summary-1',
      role: 'assistant',
      content: [
        {
          type: 'tool-call',
          toolName: WOT_SUMMARY_NAME,
          args: {
            interactions: [
              { ok: true, type: 'write_property' },
              { ok: true, type: 'invoke_action' },
              { ok: true, type: 'read_property' },
              { ok: false, type: 'write_property' },
            ],
          },
        },
      ],
    },
    {
      id: 'user-2',
      role: 'user',
      content: [{ type: 'text', text: 'Another turn' }],
    },
    {
      id: 'summary-2',
      role: 'assistant',
      content: [
        {
          type: 'tool-call',
          toolName: WOT_SUMMARY_NAME,
          args: {
            interactions: [{ ok: true, type: 'write_property' }],
          },
        },
      ],
    },
  ];

  // The summary is after the textual answer, but still belongs to its turn.
  assert.equal(countDeviceChangesForRetry(messages, 'tools-1'), 2);
});

test('retry does not warn for a read-only turn', () => {
  assert.equal(
    countDeviceChangesForRetry(
      [
        {
          id: 'user-1',
          role: 'user',
          content: [{ type: 'text', text: 'Read it' }],
        },
        {
          id: 'summary-1',
          role: 'assistant',
          content: [
            {
              type: 'tool-call',
              toolName: WOT_SUMMARY_NAME,
              args: {
                interactions: [{ ok: true, type: 'read_property' }],
              },
            },
          ],
        },
      ],
      'user-1',
    ),
    0,
  );
});
