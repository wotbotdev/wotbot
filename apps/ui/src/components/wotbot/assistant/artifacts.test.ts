import assert from 'node:assert/strict';
import test from 'node:test';

import { latestTurnArtifacts } from './artifacts';

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
