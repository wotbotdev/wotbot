import assert from 'node:assert/strict';
import { after, before, mock, test } from 'node:test';
import axios from 'axios';
import { Redis } from 'ioredis';

import { getWotClient, shutdownWot } from '../runtime/servient.js';
import { closeValkeyClient } from './valkey-client.js';
import { ensurePropertyObservation, removeSubscription, subscriptionStatus } from './subscriptions.js';

let stopped = 0;
before(async () => {
  mock.method(axios, 'get', async () => ({
    data: {
      '@context': 'https://www.w3.org/2022/wot/td/v1.1',
      id: 'urn:test:subscription',
      title: 'Subscription',
      security: ['nosec_sc'],
      securityDefinitions: { nosec_sc: { scheme: 'nosec' } },
      properties: {
        temperature: {
          type: 'number',
          observable: true,
          forms: [{ href: 'http://device/temperature', op: ['observeproperty'] }],
        },
      },
    },
  }));
  mock.method(globalThis, 'fetch', async () => Response.json({}));
  mock.method(Redis.prototype, 'connect', async () => undefined);
  mock.method(Redis.prototype, 'xadd', async () => '1-0');
  mock.method(Redis.prototype, 'quit', async () => 'OK');
  const wot = await getWotClient();
  mock.method(wot, 'consume', async () => ({
    observeProperty: async () => ({
      stop: async () => {
        stopped++;
      },
    }),
  }));
});

after(async () => {
  await closeValkeyClient();
  await shutdownWot();
  mock.restoreAll();
});

test('raw namespaces isolate reuse and cancellation from other contexts and existing callers', async () => {
  const target = { thingId: 'urn:test:subscription', affordanceName: 'temperature' };
  const first = await ensurePropertyObservation({ target, subscriptionNamespace: 'mcp-raw:first' });
  const retry = await ensurePropertyObservation({ target, subscriptionNamespace: 'mcp-raw:first' });
  const second = await ensurePropertyObservation({ target, subscriptionNamespace: 'mcp-raw:second' });
  const existing = await ensurePropertyObservation({ target });
  assert.equal(first.subscription.subscriptionId, retry.subscription.subscriptionId);
  assert.notEqual(first.subscription.subscriptionId, second.subscription.subscriptionId);
  assert.notEqual(first.subscription.subscriptionId, existing.subscription.subscriptionId);
  // Allow the asynchronous setup handlers to install their stop functions.
  for (
    let attempt = 0;
    attempt < 100 && subscriptionStatus(first.subscription.subscriptionId).status === 'pending';
    attempt++
  ) {
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
  assert.equal(subscriptionStatus(first.subscription.subscriptionId).status, 'active');
  await removeSubscription({ subscriptionId: first.subscription.subscriptionId });
  assert.equal(subscriptionStatus(first.subscription.subscriptionId).exists, false);
  assert.equal(subscriptionStatus(second.subscription.subscriptionId).exists, true);
  assert.equal(subscriptionStatus(existing.subscription.subscriptionId).exists, true);
  assert.equal(stopped, 1);
  await removeSubscription({ subscriptionId: second.subscription.subscriptionId });
  await removeSubscription({ subscriptionId: existing.subscription.subscriptionId });
});
