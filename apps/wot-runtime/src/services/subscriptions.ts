import crypto from 'node:crypto';

import { config } from '../config/env.js';
import log from '../logger/index.js';
import { annotateThingDescriptionSecurityNames } from '../runtime/credentials.js';
import { getWotClient } from '../runtime/servient.js';
import { getAffordanceDefinition, resolveFormIndex } from './form-selection.js';
import type { ThingDescription } from './thing-catalog-client.js';
import { fetchThingDescription } from './thing-catalog-client.js';
import { decodePayloadEnvelope, encodeInteractionOutputPayload } from './payloads.js';
import { createRuntimeError, formatError } from './errors.js';
import { publishRuntimeStreamEvent } from './stream-publisher.js';

/**
 * WoT operation names that involve long-lived subscriptions.
 */
type OperationName = 'observeproperty' | 'subscribeevent';

/**
 * High-level interaction type for metrics and stream events.
 */
type InteractionType = 'property' | 'event';

/**
 * Base properties for a subscription record.
 */
type SubscriptionRecordBase = {
  subscriptionId: string;
  key: string;
  thingId: string;
  name: string;
  interactionType: InteractionType;
  operationName: OperationName;
};

/**
 * Represents a subscription that is currently being established.
 */
type PendingSubscription = SubscriptionRecordBase & {
  status: 'pending';
  cancelled: boolean;
};

/**
 * Represents an established, active subscription that can be stopped.
 */
type ActiveSubscription = SubscriptionRecordBase & {
  status: 'active';
  subscription: { stop: (options?: Record<string, unknown>) => Promise<void> };
};

/**
 * Union type for all possible subscription states.
 */
type SubscriptionRecord = PendingSubscription | ActiveSubscription;

/**
 * Possible setup states for a subscription.
 */
type SubscriptionSetupState = 'pending' | 'active';

/**
 * Checks if a value is a plain object.
 */
function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

/**
 * Checks if a value has a `stop` method (i.e., is a WoT Subscription).
 */
function hasStopMethod(value: unknown): value is { stop: (options?: Record<string, unknown>) => Promise<void> } {
  if (!value || typeof value !== 'object') {
    return false;
  }

  return 'stop' in value && typeof value.stop === 'function';
}

/**
 * Checks if a property definition explicitly allows observation.
 */
function isObservablePropertyDefinition(value: unknown): boolean {
  return isPlainObject(value) && value.observable === true;
}

/**
 * Helper to wrap a promise with a timeout.
 */
async function withTimeout<T>(promise: Promise<T>, timeoutMs: number, message: string): Promise<T> {
  let timeoutId: ReturnType<typeof setTimeout> | undefined;

  try {
    return await Promise.race([
      promise,
      new Promise<T>((_, reject) => {
        timeoutId = setTimeout(() => {
          reject(createRuntimeError('deadline_exceeded', message));
        }, timeoutMs);
      }),
    ]);
  } finally {
    if (timeoutId) {
      clearTimeout(timeoutId);
    }
  }
}

/**
 * Helper to wrap a promise with an optional timeout (no-op if timeoutMs <= 0).
 */
async function withOptionalTimeout<T>(promise: Promise<T>, timeoutMs: number, message: string): Promise<T> {
  if (timeoutMs <= 0) {
    return promise;
  }

  return withTimeout(promise, timeoutMs, message);
}

/**
 * Asserts that a value is a valid WoT subscription with a `stop` method.
 */
function ensureRuntimeSubscription(
  value: unknown,
  message: string,
): { stop: (options?: Record<string, unknown>) => Promise<void> } {
  if (hasStopMethod(value)) {
    return value;
  }

  throw createRuntimeError('internal', message);
}

/**
 * Serializes a value into a stable string for use as a Map key.
 * Ensures consistent key order for objects.
 */
function stableSerialize(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map((item) => stableSerialize(item)).join(',')}]`;
  }

  if (isPlainObject(value)) {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableSerialize(value[key])}`)
      .join(',')}}`;
  }

  return JSON.stringify(value);
}

/**
 * Decodes URI variables from a runtime request.
 */
function decodeUriVariables(uriVariables: any[] | undefined): Record<string, unknown> {
  const values: Record<string, unknown> = {};

  for (const entry of Array.isArray(uriVariables) ? uriVariables : []) {
    const name = String(entry?.name || '').trim();
    if (!name) {
      continue;
    }
    values[name] = decodePayloadEnvelope(entry.value);
  }

  return values;
}

/**
 * Builds interaction options for a subscription.
 */
function buildInteractionOptions(
  request: any,
  resolvedFormIndex?: number,
  extraData?: unknown,
): Record<string, unknown> | undefined {
  const options: Record<string, unknown> = {};
  const uriVariables = decodeUriVariables(request?.uriVariables);

  if (Object.keys(uriVariables).length > 0) {
    options.uriVariables = uriVariables;
  }

  const formIndex = resolvedFormIndex ?? request?.formSelector?.formIndex;
  if (typeof formIndex === 'number' && Number.isInteger(formIndex)) {
    options.formIndex = formIndex;
  }

  if (extraData !== undefined) {
    options.data = extraData;
  }

  return Object.keys(options).length > 0 ? options : undefined;
}

/**
 * Builds a unique, stable key for a subscription based on its identity and parameters.
 */
function buildSubscriptionKey(request: any, operationName: OperationName, extraData?: unknown): string {
  return stableSerialize({
    thingId: String(request?.target?.thingId || '').trim(),
    name: String(request?.target?.affordanceName || '').trim(),
    operationName,
    namespace: String(request?.subscriptionNamespace || ''),
    uriVariables: decodeUriVariables(request?.uriVariables),
    formSelector: request?.formSelector || null,
    extraData,
  });
}

/**
 * Fetches and consumes a Thing Description.
 */
async function consumeThing(thingId: string): Promise<{
  thing: any;
  document: ThingDescription;
}> {
  const { document } = await fetchThingDescription(thingId).catch((error) => {
    throw createRuntimeError('not_found', formatError(error));
  });

  annotateThingDescriptionSecurityNames(document);
  const wot = await getWotClient();
  const thing = await wot.consume(document);
  return { thing, document };
}

/**
 * Publishes a subscription lifecycle event to the runtime event stream.
 */
async function publishSubscriptionLifecycle(
  subscription: SubscriptionRecordBase,
  eventType: 'subscription_requested' | 'subscription_started' | 'subscription_failed' | 'subscription_stopped',
  detail?: string,
): Promise<void> {
  await publishRuntimeStreamEvent({
    eventType,
    thingId: subscription.thingId,
    interactionType: subscription.interactionType,
    name: subscription.name,
    subscriptionId: subscription.subscriptionId,
    timestamp: new Date().toISOString(),
    detail,
  });
}

/**
 * Publishes a data update from an active subscription to the runtime event stream.
 */
async function publishInteractionUpdate(
  subscription: SubscriptionRecordBase,
  output: any,
  eventType: 'property_observed' | 'event_received',
): Promise<void> {
  const payload = await encodeInteractionOutputPayload(output);
  await publishRuntimeStreamEvent({
    eventType,
    thingId: subscription.thingId,
    interactionType: subscription.interactionType,
    name: subscription.name,
    subscriptionId: subscription.subscriptionId,
    deliveryId: crypto.randomUUID(),
    payloadBase64: payload.body.toString('base64'),
    contentType: payload.contentType,
    timestamp: new Date().toISOString(),
    sourceProtocol: payload.sourceProtocol,
    requiresResponse: false,
  });
}

const subscriptionsById = new Map<string, SubscriptionRecord>();
const subscriptionsByKey = new Map<string, string>();

/**
 * Builds a public-facing handle for a subscription.
 */
function buildSubscriptionHandle(subscription: SubscriptionRecordBase): {
  subscriptionId: string;
  thingId: string;
  name: string;
  operation: string;
  streamName: string;
} {
  return {
    subscriptionId: subscription.subscriptionId,
    thingId: subscription.thingId,
    name: subscription.name,
    operation:
      subscription.operationName === 'observeproperty'
        ? 'OPERATION_TYPE_OBSERVE_PROPERTY'
        : 'OPERATION_TYPE_SUBSCRIBE_EVENT',
    streamName: config.streamName,
  };
}

/**
 * Maps the internal status to a public setup state.
 */
function getSubscriptionSetupState(subscription: SubscriptionRecord): SubscriptionSetupState {
  return subscription.status;
}

/**
 * Builds the response for an "ensure" subscription request.
 */
function buildEnsureSubscriptionResponse(
  subscription: SubscriptionRecord,
  created: boolean,
): {
  subscription: ReturnType<typeof buildSubscriptionHandle>;
  created: boolean;
  setupState: SubscriptionSetupState;
} {
  return {
    subscription: buildSubscriptionHandle(subscription),
    created,
    setupState: getSubscriptionSetupState(subscription),
  };
}

/**
 * Retrieves an existing subscription that matches the given request criteria.
 */
function getExistingSubscription(
  request: any,
  operationName: OperationName,
  extraData?: unknown,
): SubscriptionRecord | undefined {
  const key = buildSubscriptionKey(request, operationName, extraData);
  const existingId = subscriptionsByKey.get(key);
  if (!existingId) {
    return undefined;
  }
  return subscriptionsById.get(existingId);
}

/**
 * Stores a subscription record in the internal tracking maps.
 */
function rememberSubscription(subscription: SubscriptionRecord): void {
  subscriptionsById.set(subscription.subscriptionId, subscription);
  subscriptionsByKey.set(subscription.key, subscription.subscriptionId);
}

/**
 * Removes a subscription record from the internal tracking maps.
 */
function forgetSubscription(subscriptionId: string): SubscriptionRecord | undefined {
  const subscription = subscriptionsById.get(subscriptionId);
  if (!subscription) {
    return undefined;
  }

  subscriptionsById.delete(subscriptionId);
  if (subscriptionsByKey.get(subscription.key) === subscriptionId) {
    subscriptionsByKey.delete(subscription.key);
  }
  return subscription;
}

/**
 * Initiates the asynchronous setup of a property observation.
 */
async function startPropertyObservation(pending: PendingSubscription, request: any): Promise<void> {
  try {
    const { thing, document } = await consumeThing(pending.thingId);
    const propertyDefinition = getAffordanceDefinition(document, pending.name, 'observeproperty');
    if (!propertyDefinition) {
      throw createRuntimeError('not_found', `Thing '${pending.thingId}' does not define property '${pending.name}'`);
    }

    if (!isObservablePropertyDefinition(propertyDefinition)) {
      throw createRuntimeError(
        'failed_precondition',
        `Thing '${pending.thingId}' property '${pending.name}' is not observable`,
      );
    }

    const resolvedFormIndex = (() => {
      try {
        return resolveFormIndex(document, pending.name, 'observeproperty', request?.formSelector);
      } catch (error) {
        throw createRuntimeError('invalid_argument', formatError(error));
      }
    })();

    const options = buildInteractionOptions(request, resolvedFormIndex);
    const subscription = ensureRuntimeSubscription(
      await withOptionalTimeout(
        thing.observeProperty(
          pending.name,
          (output: any) => {
            void publishInteractionUpdate(pending, output, 'property_observed').catch((error) => {
              const message = formatError(error);
              log.error(`Failed to publish property observation: ${message}`);
            });
          },
          (error: unknown) => {
            void publishSubscriptionLifecycle(pending, 'subscription_failed', formatError(error));
          },
          options,
        ),
        config.subscriptionSetupTimeoutMs,
        `Timed out while observing property '${pending.name}' on Thing '${pending.thingId}'`,
      ),
      `Observation for property '${pending.name}' on Thing '${pending.thingId}' did not return a stoppable subscription`,
    );

    const current = subscriptionsById.get(pending.subscriptionId);
    if (current !== pending || pending.cancelled) {
      await subscription.stop().catch(() => undefined);
      if (current === pending) {
        forgetSubscription(pending.subscriptionId);
      }
      return;
    }

    const active: ActiveSubscription = {
      ...pending,
      status: 'active',
      subscription,
    };
    rememberSubscription(active);
    await publishSubscriptionLifecycle(active, 'subscription_started');
  } catch (error) {
    const current = subscriptionsById.get(pending.subscriptionId);
    if (current === pending) {
      forgetSubscription(pending.subscriptionId);
    }

    if (pending.cancelled) {
      return;
    }

    const message = formatError(error);
    log.warn(`Failed to start property observation: ${message}`);
    await publishSubscriptionLifecycle(pending, 'subscription_failed', message).catch(() => undefined);
  }
}

/**
 * Initiates the asynchronous setup of an event subscription.
 */
async function startEventSubscription(
  pending: PendingSubscription,
  request: any,
  subscriptionInput: unknown,
): Promise<void> {
  try {
    const { thing, document } = await consumeThing(pending.thingId);
    const eventDefinition = getAffordanceDefinition(document, pending.name, 'subscribeevent');
    if (!eventDefinition) {
      throw createRuntimeError('not_found', `Thing '${pending.thingId}' does not define event '${pending.name}'`);
    }

    const resolvedFormIndex = (() => {
      try {
        return resolveFormIndex(document, pending.name, 'subscribeevent', request?.formSelector);
      } catch (error) {
        throw createRuntimeError('invalid_argument', formatError(error));
      }
    })();

    const options = buildInteractionOptions(request, resolvedFormIndex, subscriptionInput);
    const subscription = ensureRuntimeSubscription(
      await withOptionalTimeout(
        thing.subscribeEvent(
          pending.name,
          (output: any) => {
            void publishInteractionUpdate(pending, output, 'event_received').catch((error) => {
              const message = formatError(error);
              log.error(`Failed to publish event subscription data: ${message}`);
            });
          },
          (error: unknown) => {
            void publishSubscriptionLifecycle(pending, 'subscription_failed', formatError(error));
          },
          options,
        ),
        config.subscriptionSetupTimeoutMs,
        `Timed out while subscribing to event '${pending.name}' on Thing '${pending.thingId}'`,
      ),
      `Event subscription for '${pending.name}' on Thing '${pending.thingId}' did not return a stoppable subscription`,
    );

    const current = subscriptionsById.get(pending.subscriptionId);
    if (current !== pending || pending.cancelled) {
      await subscription.stop().catch(() => undefined);
      if (current === pending) {
        forgetSubscription(pending.subscriptionId);
      }
      return;
    }

    const active: ActiveSubscription = {
      ...pending,
      status: 'active',
      subscription,
    };
    rememberSubscription(active);
    await publishSubscriptionLifecycle(active, 'subscription_started');
  } catch (error) {
    const current = subscriptionsById.get(pending.subscriptionId);
    if (current === pending) {
      forgetSubscription(pending.subscriptionId);
    }

    if (pending.cancelled) {
      return;
    }

    const message = formatError(error);
    log.warn(`Failed to start event subscription: ${message}`);
    await publishSubscriptionLifecycle(pending, 'subscription_failed', message).catch(() => undefined);
  }
}

/**
 * Ensures that a property observation exists for the given target.
 * If a matching observation already exists, it is returned.
 * Otherwise, a new one is asynchronously established.
 *
 * @param request The runtime request containing target and options.
 * @returns A promise resolving to a subscription handle and its status.
 */
export async function ensurePropertyObservation(request: any): Promise<any> {
  const thingId = String(request?.target?.thingId || '').trim();
  const propertyName = String(request?.target?.affordanceName || '').trim();

  if (!thingId) {
    throw createRuntimeError('invalid_argument', 'thing_id is required');
  }
  if (!propertyName) {
    throw createRuntimeError('invalid_argument', 'target.affordance_name is required for EnsurePropertyObservation');
  }

  const existing = getExistingSubscription(request, 'observeproperty');
  if (existing) {
    return buildEnsureSubscriptionResponse(existing, false);
  }

  const key = buildSubscriptionKey(request, 'observeproperty');
  const pending: PendingSubscription = {
    subscriptionId: crypto.randomUUID(),
    key,
    thingId,
    name: propertyName,
    interactionType: 'property',
    operationName: 'observeproperty',
    status: 'pending',
    cancelled: false,
  };

  rememberSubscription(pending);
  void publishSubscriptionLifecycle(pending, 'subscription_requested').catch(() => undefined);
  void startPropertyObservation(pending, request);

  return buildEnsureSubscriptionResponse(pending, true);
}

/**
 * Ensures that an event subscription exists for the given target.
 * If a matching subscription already exists, it is returned.
 * Otherwise, a new one is asynchronously established.
 *
 * @param request The runtime request containing target, input, and options.
 * @returns A promise resolving to a subscription handle and its status.
 */
export async function ensureEventSubscription(request: any): Promise<any> {
  const thingId = String(request?.target?.thingId || '').trim();
  const eventName = String(request?.target?.affordanceName || '').trim();

  if (!thingId) {
    throw createRuntimeError('invalid_argument', 'thing_id is required');
  }
  if (!eventName) {
    throw createRuntimeError('invalid_argument', 'target.affordance_name is required for EnsureEventSubscription');
  }

  const subscriptionInput = decodePayloadEnvelope(request?.subscriptionInput);
  const existing = getExistingSubscription(request, 'subscribeevent', subscriptionInput);
  if (existing) {
    return buildEnsureSubscriptionResponse(existing, false);
  }

  const key = buildSubscriptionKey(request, 'subscribeevent', subscriptionInput);
  const pending: PendingSubscription = {
    subscriptionId: crypto.randomUUID(),
    key,
    thingId,
    name: eventName,
    interactionType: 'event',
    operationName: 'subscribeevent',
    status: 'pending',
    cancelled: false,
  };

  rememberSubscription(pending);
  void publishSubscriptionLifecycle(pending, 'subscription_requested').catch(() => undefined);
  void startEventSubscription(pending, request, subscriptionInput);

  return buildEnsureSubscriptionResponse(pending, true);
}

/**
 * Removes and stops a specific subscription by its ID.
 *
 * @param request The runtime request containing the subscriptionId.
 * @returns A promise resolving to whether the subscription was removed.
 */
export async function removeSubscription(request: any): Promise<any> {
  const subscriptionId = String(request?.subscriptionId || '').trim();
  if (!subscriptionId) {
    throw createRuntimeError('invalid_argument', 'subscription_id is required for RemoveSubscription');
  }

  const subscription = forgetSubscription(subscriptionId);
  if (!subscription) {
    return { removed: false };
  }

  if (subscription.status === 'pending') {
    subscription.cancelled = true;
    return { removed: true };
  }

  const cancellationInput = decodePayloadEnvelope(request?.cancellationInput);
  const options = cancellationInput === undefined ? undefined : { data: cancellationInput };

  try {
    await subscription.subscription.stop(options);
  } catch (error) {
    throw createRuntimeError('unknown', formatError(error));
  }

  await publishSubscriptionLifecycle(subscription, 'subscription_stopped');
  return { removed: true };
}

/**
 * Stops all currently tracked subscriptions.
 * Used during runtime shutdown.
 */
export async function stopAllSubscriptions(): Promise<void> {
  const activeSubscriptions = [...subscriptionsById.values()];
  subscriptionsById.clear();
  subscriptionsByKey.clear();

  await Promise.all(
    activeSubscriptions.map(async (subscription) => {
      if (subscription.status === 'pending') {
        subscription.cancelled = true;
        return;
      }

      await subscription.subscription.stop().catch(() => undefined);
      await publishSubscriptionLifecycle(subscription, 'subscription_stopped').catch(() => undefined);
    }),
  );
}

/** Inspect a handle without creating/restarting its subscription. */
export function subscriptionStatus(subscriptionId: string): { exists: boolean; status?: string } {
  const subscription = subscriptionsById.get(subscriptionId);
  return subscription ? { exists: true, status: subscription.status } : { exists: false };
}
