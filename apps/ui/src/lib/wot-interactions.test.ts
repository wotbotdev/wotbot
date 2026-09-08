import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DEVICE_INTERACTION_SUMMARY_TYPE,
  parseDeviceInteractionSummaryContent,
  parseWotInteractionList,
} from './wot-interactions';

test('parseWotInteractionList reads stringified run_code output', () => {
  const interactions = parseWotInteractionList(
    JSON.stringify({
      stdout: 'done',
      wot_calls: [
        {
          type: 'write_property',
          thing_id: 'urn:wotbot:thing:packaging-conveyor',
          name: 'targetSpeed',
          ok: true,
          uri_variables: { zone: 'north' },
          value: 22,
        },
      ],
    }),
  );

  assert.deepEqual(interactions, [
    {
      affordanceName: 'targetSpeed',
      ok: true,
      thingId: 'urn:wotbot:thing:packaging-conveyor',
      type: 'write_property',
      uriVariables: { zone: 'north' },
      value: 22,
    },
  ]);
});

test('parseDeviceInteractionSummaryContent reads graph summary marker content', () => {
  assert.deepEqual(
    parseDeviceInteractionSummaryContent(
      JSON.stringify({
        type: DEVICE_INTERACTION_SUMMARY_TYPE,
        interactions: [
          {
            type: 'write_property',
            thingId: 'urn:wotbot:thing:parcel-sorter',
            affordanceName: 'throughputLimit',
            ok: true,
            uriVariables: { channel: 1 },
            value: 40,
          },
        ],
      }),
    ),
    [
      {
        affordanceName: 'throughputLimit',
        ok: true,
        thingId: 'urn:wotbot:thing:parcel-sorter',
        type: 'write_property',
        uriVariables: { channel: 1 },
        value: 40,
      },
    ],
  );
});
