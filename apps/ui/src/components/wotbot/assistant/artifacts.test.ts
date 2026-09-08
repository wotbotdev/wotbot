import assert from 'node:assert/strict';
import test from 'node:test';

import {
  conversationArtifacts,
  conversationFiles,
  latestTurnArtifacts,
} from './artifacts';
import { toThreadMessages, type LangChainMessage } from '@/lib/thread-messages';
import { artifactKey } from '../chat-tool-call-model';

test('latestTurnArtifacts includes and enriches voice-created panels', () => {
  const artifacts = latestTurnArtifacts([
    { type: 'human', content: 'Build a conveyor panel', id: 'human-1' },
    {
      type: 'ai',
      content: '',
      tool_calls: [
        {
          id: 'call-panel',
          name: 'create_web_interface',
          args: {
            html: '<button>Pause conveyor</button>',
            title: 'Conveyor controls',
          },
        },
      ],
    },
    {
      type: 'tool',
      tool_call_id: 'call-panel',
      content: JSON.stringify({
        artifacts: [
          {
            ref: 'ui_1',
            kind: 'web',
            filename: 'conveyor-panel.html',
            capabilities: [
              {
                thingId: 'urn:conveyor',
                affordances: ['running'],
                ops: ['writeProperty'],
              },
            ],
          },
        ],
      }),
    },
    { type: 'ai', content: 'The conveyor controls are ready.' },
  ]);

  assert.deepEqual(artifacts, [
    {
      ref: 'ui_1',
      kind: 'web',
      filename: 'conveyor-panel.html',
      capabilities: [
        {
          thingId: 'urn:conveyor',
          affordances: ['running'],
          ops: ['writeProperty'],
        },
      ],
      html: '<button>Pause conveyor</button>',
      title: 'Conveyor controls',
    },
  ]);
});

test('latestTurnArtifacts still includes plots from only the latest turn', () => {
  const artifacts = latestTurnArtifacts([
    { type: 'human', content: 'Old chart', id: 'human-1' },
    {
      type: 'tool',
      content: JSON.stringify({
        artifacts: [
          { ref: 'chart_1', kind: 'plotly', filename: 'old-chart.json' },
        ],
      }),
    },
    { type: 'human', content: 'New chart', id: 'human-2' },
    {
      type: 'tool',
      content: JSON.stringify({
        artifacts: [
          { ref: 'chart_2', kind: 'plotly', filename: 'new-chart.json' },
        ],
      }),
    },
  ]);

  assert.deepEqual(artifacts, [
    { ref: 'chart_2', kind: 'plotly', filename: 'new-chart.json' },
  ]);
});

test('conversation artifacts span turns and link to the displayed source message without duplicate display parts', () => {
  const file = {
    kind: 'file',
    id: 'file-first.csv',
    filename: 'data.csv',
    ref: 'file_1',
  };
  const messages: LangChainMessage[] = [
    { type: 'human', id: 'human-1', content: 'Export a table' },
    { type: 'ai', id: 'turn-1', content: 'I will export the table.' },
    {
      type: 'ai',
      id: 'step-2',
      tool_calls: [{ id: 'export-1', name: 'run_code', args: {} }],
    },
    {
      type: 'tool',
      tool_call_id: 'export-1',
      content: JSON.stringify({ artifacts: [file] }),
    },
    {
      type: 'human',
      id: 'human-2',
      content: 'Export another table and a chart',
    },
    {
      type: 'ai',
      id: 'turn-2',
      tool_calls: [{ id: 'export-2', name: 'run_code', args: {} }],
    },
    {
      type: 'tool',
      tool_call_id: 'export-2',
      content: {
        artifacts: [
          file,
          { ...file, id: 'file-second.csv' },
          { kind: 'plotly', ref: 'chart_1', filename: 'plot.json' },
        ],
      },
    },
  ];
  const entries = conversationArtifacts(toThreadMessages(messages));
  assert.deepEqual(
    entries.map(({ artifact, messageId }) => [
      artifactKey(artifact),
      messageId,
    ]),
    [
      ['plotly:plot.json', 'turn-2'],
      ['file:file-second.csv', 'turn-2'],
      ['file:file-first.csv', 'turn-1'],
    ],
  );
  // Editing away the second turn also removes its outputs from the drawer.
  assert.equal(
    conversationArtifacts(toThreadMessages(messages.slice(0, 4))).length,
    1,
  );
});

test('conversation artifacts ignore unfinished and failed tools and retain panel previews', () => {
  const entries = conversationArtifacts(
    toThreadMessages([
      { type: 'human', id: 'human-1', content: 'Create some outputs' },
      {
        type: 'ai',
        id: 'turn-1',
        tool_calls: [
          { id: 'pending', name: 'run_code', args: {} },
          { id: 'failed', name: 'run_code', args: {} },
          {
            id: 'panel',
            name: 'create_web_interface',
            args: { title: 'Overview', html: '<p>Overview</p>' },
          },
        ],
      },
      {
        type: 'tool',
        tool_call_id: 'failed',
        status: 'error',
        content: {
          artifacts: [
            { kind: 'image', ref: 'image_1', filename: 'failed.png' },
          ],
        },
      },
      {
        type: 'tool',
        tool_call_id: 'panel',
        content: {
          artifacts: [
            {
              kind: 'web',
              ref: 'ui_1',
              filename: 'panel.html',
              capabilities: [],
            },
          ],
        },
      },
    ]),
  );
  assert.deepEqual(entries, [
    {
      messageId: 'turn-1',
      artifact: {
        kind: 'web',
        ref: 'ui_1',
        filename: 'panel.html',
        capabilities: [],
        title: 'Overview',
        html: '<p>Overview</p>',
      },
    },
  ]);
  assert.deepEqual(conversationArtifacts([]), []);
});

test('conversation files keep only downloadable artifacts, newest first', () => {
  const entries = conversationFiles(
    toThreadMessages([
      { type: 'human', id: 'human-1', content: 'Export the readings' },
      {
        type: 'ai',
        id: 'turn-1',
        tool_calls: [{ id: 'export-1', name: 'run_code', args: {} }],
      },
      {
        type: 'tool',
        tool_call_id: 'export-1',
        content: {
          artifacts: [
            { kind: 'plotly', ref: 'chart_1', filename: 'plot.json' },
            { kind: 'image', ref: 'image_1', filename: 'shot.png' },
            {
              kind: 'file',
              ref: 'file_1',
              id: 'file-first',
              filename: 'readings.csv',
            },
          ],
        },
      },
      { type: 'human', id: 'human-2', content: 'And the summary' },
      {
        type: 'ai',
        id: 'turn-2',
        tool_calls: [{ id: 'export-2', name: 'run_code', args: {} }],
      },
      {
        type: 'tool',
        tool_call_id: 'export-2',
        content: {
          artifacts: [
            {
              kind: 'file',
              ref: 'file_1',
              id: 'file-second',
              filename: 'summary.csv',
            },
          ],
        },
      },
    ]),
  );
  assert.deepEqual(
    entries.map(({ artifact, messageId }) => [artifact.id, messageId]),
    [
      ['file-second', 'turn-2'],
      ['file-first', 'turn-1'],
    ],
  );
  assert.deepEqual(conversationFiles([]), []);
});
