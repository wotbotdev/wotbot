import assert from 'node:assert/strict';
import test from 'node:test';

import {
  isCacheableSafeAction,
  missingInvokeActionInputMessage,
  resolveInvokeActionInput,
  resolveInvokeActionParameters,
} from './operations.js';

test('resolveInvokeActionInput defaults missing optional object input to empty object', () => {
  const actionDef = {
    input: {
      type: 'object',
      properties: {
        from_ms: { type: 'integer' },
        to_ms: { type: 'integer' },
      },
    },
  };

  assert.deepEqual(resolveInvokeActionInput(actionDef, undefined), {});
  assert.deepEqual(resolveInvokeActionInput(actionDef, null), {});
});

test('resolveInvokeActionInput preserves provided input values', () => {
  const actionDef = {
    input: {
      type: 'object',
      properties: {
        from_ms: { type: 'integer' },
      },
    },
  };

  assert.deepEqual(resolveInvokeActionInput(actionDef, { from_ms: 123 }), { from_ms: 123 });
});

test('missingInvokeActionInputMessage explains required object input', () => {
  const actionDef = {
    input: {
      type: 'object',
      required: ['from_ms', 'to_ms'],
      properties: {
        from_ms: { type: 'integer' },
        to_ms: { type: 'integer' },
      },
    },
  };

  assert.equal(
    missingInvokeActionInputMessage(actionDef, 'virtual:things:energy', 'analyze', undefined),
    "InvokeAction input for 'virtual:things:energy/analyze' must be an object with required fields: from_ms, to_ms. Pass an object matching the Thing Description input schema.",
  );
});

test('missingInvokeActionInputMessage ignores optional object input', () => {
  const actionDef = {
    input: {
      type: 'object',
      properties: {
        from_ms: { type: 'integer' },
      },
    },
  };

  assert.equal(missingInvokeActionInputMessage(actionDef, 'virtual:things:energy', 'analyze', undefined), null);
});

test('isCacheableSafeAction only accepts explicit safe actions', () => {
  assert.equal(isCacheableSafeAction({ safe: true }), true);
  assert.equal(isCacheableSafeAction({ safe: false }), false);
  assert.equal(isCacheableSafeAction({}), false);
  assert.equal(isCacheableSafeAction(null), false);
});

test('GET and HEAD recover only declared parameters, including Thing-level variables', () => {
  for (const method of ['GET', 'HEAD']) {
    for (const href of ['https://api.example/{city}', 'wotbot+provider://runtime/actions/query{?city}']) {
      const document = {
        uriVariables: { language: { type: 'string' } },
        actions: {
          query: {
            uriVariables: { city: { type: 'integer' }, enabled: { type: 'boolean' } },
            forms: [{ href, 'htv:methodName': method }],
          },
        },
      };
      const input = { city: 0, language: '', enabled: false, undeclared: 'ignore' };
      assert.deepEqual(resolveInvokeActionParameters(document, 'query', undefined, input, {}), {
        input: undefined,
        uriVariables: { city: 0, language: '', enabled: false },
      });
      assert.equal(input.undeclared, 'ignore');
      assert.deepEqual(resolveInvokeActionParameters(document, 'query', undefined, input, { city: 42 }), {
        input: undefined,
        uriVariables: { city: 42 },
      });
      for (const nonObject of [null, undefined, 'city=1', [1], Buffer.from('city=1')]) {
        assert.deepEqual(resolveInvokeActionParameters(document, 'query', undefined, nonObject, {}), {
          input: undefined,
          uriVariables: {},
        });
      }
    }
  }
});

test('POST and non-HTTP actions keep body fields that also name URI variables', () => {
  for (const form of [
    { href: 'https://api.example/query{?city}', 'htv:methodName': 'POST' },
    { href: 'https://api.example/query{?city}' },
    { href: 'wotbot+provider://runtime/actions/query{?city}', 'htv:methodName': 'POST' },
    { href: 'coap://device.example/query', 'htv:methodName': 'GET' },
    { href: 'mcp+http://device.example/query', 'htv:methodName': 'GET' },
  ]) {
    const document = {
      actions: { query: { uriVariables: { city: { type: 'integer' } }, forms: [form] } },
    };
    const input = { city: 12 };
    assert.deepEqual(resolveInvokeActionParameters(document, 'query', undefined, input, {}), {
      input,
      uriVariables: {},
    });
  }
});

test('parameter recovery follows the selected form and resolves relative HTTP forms', () => {
  const document = {
    base: 'https://api.example/',
    actions: {
      query: {
        uriVariables: { city: { type: 'integer' } },
        forms: [
          { href: 'query', op: ['invokeaction'], 'htv:methodName': 'POST' },
          { href: 'query{?city}', op: ['invokeaction'], 'htv:methodName': 'GET' },
        ],
      },
    },
  };
  assert.deepEqual(resolveInvokeActionParameters(document, 'query', 1, { city: 12 }, {}), {
    input: undefined,
    uriVariables: { city: 12 },
  });
  assert.deepEqual(resolveInvokeActionParameters(document, 'query', 0, { city: 12 }, {}), {
    input: { city: 12 },
    uriVariables: {},
  });
});
