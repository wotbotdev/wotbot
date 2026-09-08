import log from '../logger/index.js';
import { annotateThingDescriptionSecurityNames } from '../runtime/credentials.js';
import { ensureWotReady, getServient, getWotClient } from '../runtime/servient.js';
import { buildCacheKey, getCached, setCached } from '../services/cache.js';
import { fetchThingDescription, type ThingDescription } from '../services/thing-catalog-client.js';
import {
  decodePayloadEnvelope,
  encodeInteractionOutputPayload,
  encodePayloadEnvelope,
  normalizeBody,
  rejectHtmlPayload,
} from '../services/payloads.js';
import { createRuntimeError, formatError, isDataSchemaError } from '../services/errors.js';
import { getAffordanceDefinition, getFormHttpMethod, resolveFormIndex } from '../services/form-selection.js';
import { getRuntimeHealth } from '../services/runtime-health.js';

/**
 * Checks if a value is a plain object.
 */
function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

/**
 * Extracts the thingId from a runtime request.
 */
function getRequestedThingId(request: any): string {
  return String(request?.target?.thingId || request?.thingId || '').trim();
}

/**
 * Decodes URI variables from a runtime request.
 */
function decodeUriVariables(uriVariables: any[] | undefined): Record<string, unknown> {
  const entries = Array.isArray(uriVariables) ? uriVariables : [];
  const values: Record<string, unknown> = {};

  for (const entry of entries) {
    const name = String(entry?.name || '').trim();
    if (!name) {
      continue;
    }
    values[name] = decodePayloadEnvelope(entry.value);
  }

  return values;
}

/**
 * Builds interaction options (uriVariables, formIndex) for node-wot.
 */
function buildInteractionOptions(request: any, resolvedFormIndex?: number): Record<string, unknown> | undefined {
  const options: Record<string, unknown> = {};
  const uriVariables = decodeUriVariables(request.uriVariables);

  if (Object.keys(uriVariables).length > 0) {
    options.uriVariables = uriVariables;
  }

  const formIndex = resolvedFormIndex ?? request?.formSelector?.formIndex;
  if (typeof formIndex === 'number' && Number.isInteger(formIndex)) {
    options.formIndex = formIndex;
  }

  return Object.keys(options).length === 0 ? undefined : options;
}

/**
 * Builds a standardized interaction response object from an encoded payload.
 */
function buildEncodedInteractionResponse(
  payload: { body: Buffer; contentType: string },
  responseContentType?: string,
): { response: any } {
  const normalizedResponseContentType = responseContentType || payload.contentType || 'application/json';

  return {
    response: {
      payload: {
        body: payload.body,
        contentType: payload.contentType,
      },
      responseContentType: normalizedResponseContentType,
      matchedAdditionalResponse: false,
      success: true,
      statusCode: 200,
      statusText: 'ok',
      chosenForm: {},
    },
  };
}

/**
 * Builds a standardized interaction response object from a high-level value.
 */
function buildInteractionResponse(value: unknown, contentType?: string): { response: any } {
  const payload = encodePayloadEnvelope(value, contentType);
  return buildEncodedInteractionResponse(
    {
      body: normalizeBody(payload.body),
      contentType: String(payload.contentType || contentType || 'application/json'),
    },
    contentType || String(payload.contentType || 'application/json'),
  );
}

function interactionError(operation: string, thingId: string, affordanceName: string, error: unknown): never {
  if (isDataSchemaError(error) || formatError(error) === 'Invalid value according to DataSchema') {
    throw createRuntimeError(
      'invalid_argument',
      `${operation} input for '${thingId}/${affordanceName}' does not match the Thing Description schema. ` +
        `Check the affordance input schema and pass a matching value, or remove the input schema for a no-argument action. ` +
        `Original error: ${formatError(error)}`,
    );
  }
  throw error;
}

function schemaIncludesType(schema: Record<string, unknown>, expectedType: string): boolean {
  const type = schema.type;
  if (type === expectedType) {
    return true;
  }
  return Array.isArray(type) && type.includes(expectedType);
}

function actionInputSchema(actionDef: unknown): Record<string, unknown> | null {
  if (!isPlainObject(actionDef) || !isPlainObject(actionDef.input)) {
    return null;
  }
  return actionDef.input;
}

function objectInputRequiredFields(actionDef: unknown): string[] | null {
  const schema = actionInputSchema(actionDef);
  if (!schema || !schemaIncludesType(schema, 'object')) {
    return null;
  }
  return Array.isArray(schema.required)
    ? schema.required.filter((field): field is string => typeof field === 'string' && field.trim().length > 0)
    : [];
}

/**
 * Defaults omitted optional object action inputs to an empty object.
 */
export function resolveInvokeActionInput(actionDef: unknown, input: unknown): unknown {
  const requiredFields = objectInputRequiredFields(actionDef);
  if (requiredFields === null) {
    return input;
  }
  if ((input === undefined || input === null) && requiredFields.length === 0) {
    return {};
  }
  return input;
}

/**
 * Recover declared URI parameters supplied as a GET/HEAD body before dropping it.
 * Explicit URI variables take precedence. Other methods and bindings keep their input.
 */
export function resolveInvokeActionParameters(
  document: ThingDescription,
  actionName: string,
  formIndex: number | undefined,
  input: unknown,
  uriVariables: Record<string, unknown>,
): { input: unknown; uriVariables: Record<string, unknown> } {
  const method = getFormHttpMethod(document, actionName, 'invokeaction', formIndex);
  if (method !== 'GET' && method !== 'HEAD') {
    return { input, uriVariables };
  }

  const action = getAffordanceDefinition(document, actionName, 'invokeaction');
  const declared = {
    ...(isPlainObject(document.uriVariables) ? document.uriVariables : {}),
    ...(isPlainObject(action?.uriVariables) ? action.uriVariables : {}),
  };
  const resolvedVariables =
    Object.keys(uriVariables).length === 0 && isPlainObject(input) && !Buffer.isBuffer(input)
      ? Object.fromEntries(Object.entries(input).filter(([key]) => Object.hasOwn(declared, key)))
      : uriVariables;

  return { input: undefined, uriVariables: resolvedVariables };
}

/**
 * Builds a clear validation message for omitted required object action inputs.
 */
export function missingInvokeActionInputMessage(
  actionDef: unknown,
  thingId: string,
  actionName: string,
  input: unknown,
): string | null {
  if (input !== undefined && input !== null) {
    return null;
  }

  const requiredFields = objectInputRequiredFields(actionDef);
  if (!requiredFields?.length) {
    return null;
  }

  return (
    `InvokeAction input for '${thingId}/${actionName}' must be an object with required ` +
    `field${requiredFields.length === 1 ? '' : 's'}: ${requiredFields.join(', ')}. ` +
    `Pass an object matching the Thing Description input schema.`
  );
}

/**
 * Returns whether an action is marked `safe` in its Thing Description and is
 * therefore idempotent enough to cache its result.
 */
export function isCacheableSafeAction(actionDef: unknown): boolean {
  return isPlainObject(actionDef) && actionDef.safe === true;
}

/**
 * Fetches a Thing Description and consumes it via the node-wot servient.
 */
async function consumeThing(request: any): Promise<{
  thing: any;
  document: ThingDescription;
  hash: string;
}> {
  const thingId = getRequestedThingId(request);
  if (!thingId) {
    throw createRuntimeError('invalid_argument', 'thing_id is required');
  }

  const { document, hash } = await fetchThingDescription(thingId).catch((error) => {
    throw createRuntimeError('not_found', formatError(error));
  });

  annotateThingDescriptionSecurityNames(document);
  const wot = await getWotClient();
  const thing = await wot.consume(document);

  return { thing, document, hash };
}

/**
 * Handles a request to retrieve a Thing Description.
 *
 * @param request The runtime request containing the thingId.
 */
export async function handleGetThingDescription(request: any): Promise<any> {
  const thingId = String(request?.thingId || '').trim();
  if (!thingId) {
    throw createRuntimeError('invalid_argument', 'thing_id is required');
  }

  const { document, hash } = await fetchThingDescription(thingId).catch((error) => {
    throw createRuntimeError('not_found', formatError(error));
  });

  return {
    thingId,
    thingDescription: encodePayloadEnvelope(document, 'application/td+json'),
    tdHash: hash,
  };
}

/**
 * Handles a ReadProperty interaction.
 *
 * @param request The runtime request containing target and options.
 */
export async function handleReadProperty(request: any): Promise<any> {
  const thingId = getRequestedThingId(request);
  const propertyName = String(request?.target?.affordanceName || '').trim();
  if (!propertyName) {
    throw createRuntimeError('invalid_argument', 'target.affordance_name is required for ReadProperty');
  }
  const { thing, document } = await consumeThing(request);
  if (!getAffordanceDefinition(document, propertyName, 'readproperty')) {
    throw createRuntimeError('not_found', `Thing '${thingId}' does not define property '${propertyName}'`);
  }

  const resolvedFormIndex = (() => {
    try {
      return resolveFormIndex(document, propertyName, 'readproperty', request?.formSelector);
    } catch (error) {
      throw createRuntimeError('invalid_argument', formatError(error));
    }
  })();

  const options = buildInteractionOptions(request, resolvedFormIndex);
  const result = await thing
    .readProperty(propertyName, options)
    .catch((error: unknown) => interactionError('ReadProperty', thingId, propertyName, error));

  const payload = await encodeInteractionOutputPayload(result, {
    onInvalidSchema: () => {
      log.warn(`Property '${propertyName}' returned data that failed schema validation, returning raw value`);
    },
  });

  return buildEncodedInteractionResponse({ body: payload.body, contentType: payload.contentType }, payload.contentType);
}

/**
 * Handles a WriteProperty interaction.
 *
 * @param request The runtime request containing target, input, and options.
 */
export async function handleWriteProperty(request: any): Promise<any> {
  const thingId = getRequestedThingId(request);
  const propertyName = String(request?.target?.affordanceName || '').trim();
  if (!propertyName) {
    throw createRuntimeError('invalid_argument', 'target.affordance_name is required for WriteProperty');
  }
  const input = decodePayloadEnvelope(request.input);
  if (input === undefined) {
    throw createRuntimeError('invalid_argument', 'input payload is required for WriteProperty');
  }

  const { thing, document } = await consumeThing(request);
  if (!getAffordanceDefinition(document, propertyName, 'writeproperty')) {
    throw createRuntimeError('not_found', `Thing '${thingId}' does not define property '${propertyName}'`);
  }

  const resolvedFormIndex = (() => {
    try {
      return resolveFormIndex(document, propertyName, 'writeproperty', request?.formSelector);
    } catch (error) {
      throw createRuntimeError('invalid_argument', formatError(error));
    }
  })();

  const options = buildInteractionOptions(request, resolvedFormIndex);
  await thing
    .writeProperty(propertyName, input, options)
    .catch((error: unknown) => interactionError('WriteProperty', thingId, propertyName, error));

  return buildInteractionResponse(undefined);
}

/**
 * Handles an InvokeAction interaction.
 *
 * @param request The runtime request containing target, input, and options.
 */
export async function handleInvokeAction(request: any): Promise<any> {
  const thingId = getRequestedThingId(request);
  const actionName = String(request?.target?.affordanceName || '').trim();
  if (!actionName) {
    throw createRuntimeError('invalid_argument', 'target.affordance_name is required for InvokeAction');
  }
  const { thing, document } = await consumeThing(request);
  if (!getAffordanceDefinition(document, actionName, 'invokeaction')) {
    throw createRuntimeError('not_found', `Thing '${thingId}' does not define action '${actionName}'`);
  }

  const resolvedFormIndex = (() => {
    try {
      return resolveFormIndex(document, actionName, 'invokeaction', request?.formSelector);
    } catch (error) {
      throw createRuntimeError('invalid_argument', formatError(error));
    }
  })();

  const options = buildInteractionOptions(request, resolvedFormIndex) || {};
  const actionDef = getAffordanceDefinition(document, actionName, 'invokeaction');
  const decodedInput = decodePayloadEnvelope(request.input);
  const missingInputMessage = missingInvokeActionInputMessage(actionDef, thingId, actionName, decodedInput);
  if (missingInputMessage) {
    throw createRuntimeError('invalid_argument', missingInputMessage);
  }
  const resolvedInput = resolveInvokeActionInput(actionDef, decodedInput);

  const { input, uriVariables } = resolveInvokeActionParameters(
    document,
    actionName,
    resolvedFormIndex,
    resolvedInput,
    decodeUriVariables(request.uriVariables),
  );
  if (Object.keys(uriVariables).length > 0) {
    options.uriVariables = uriVariables;
  }

  if (isPlainObject(actionDef) && actionDef.synchronous === false) {
    throw createRuntimeError(
      'unimplemented',
      `Action '${actionName}' declares synchronous=false and query/cancel support is not implemented yet`,
    );
  }

  const isCacheable = isCacheableSafeAction(actionDef);
  const cacheKey = isCacheable ? buildCacheKey(thingId, 'invoke_action', actionName, uriVariables, input) : '';

  if (isCacheable) {
    const cached = await getCached(cacheKey);
    if (cached) {
      const body = Buffer.from(cached.payload, 'base64');
      rejectHtmlPayload(body, cached.contentType);
      log.info(`Cache hit for invokeAction '${thingId}/${actionName}'`);
      return {
        completedResult: buildEncodedInteractionResponse({ body, contentType: cached.contentType }, cached.contentType)
          .response,
      };
    }
  }

  const result = await (
    input === undefined
      ? thing.invokeAction(actionName, undefined, options)
      : thing.invokeAction(actionName, input, options)
  ).catch((error: unknown) => interactionError('InvokeAction', thingId, actionName, error));

  if (result) {
    const payload = await encodeInteractionOutputPayload(result, {
      onInvalidSchema: () => {
        log.warn(`Action '${actionName}' returned data that failed output schema validation, returning raw value`);
      },
    });
    const interactionResponse = buildEncodedInteractionResponse(
      { body: payload.body, contentType: payload.contentType },
      payload.contentType,
    );

    if (isCacheable) {
      await setCached(
        cacheKey,
        { contentType: payload.contentType, payload: payload.body.toString('base64'), statusCode: 200 },
        payload.body.length,
      ).catch((error) =>
        log.warn(`Cache write failed for invokeAction '${thingId}/${actionName}': ${formatError(error)}`),
      );
    }

    return { completedResult: interactionResponse.response };
  }

  return {
    completedResult: buildInteractionResponse(undefined).response,
  };
}

/**
 * Builds a Thing Description for an endpoint that describes itself.
 *
 * The protocol client is chosen by the URL's scheme, exactly as it is for any other
 * interaction, so this stays generic: any binding implementing node-wot's
 * `requestThingDescription` is reachable here without a change to this function.
 *
 * The endpoint is contacted unauthenticated — there is no Thing yet, so there are no
 * stored credentials to look up. Endpoints requiring auth must be described by hand.
 *
 * @param request The runtime request carrying the endpoint url.
 */
export async function handleDescribeEndpoint(request: any): Promise<any> {
  const url = String(request?.url || '').trim();
  if (!url) {
    throw createRuntimeError('invalid_argument', 'url is required');
  }

  let scheme: string;
  try {
    scheme = new URL(url).protocol.replace(/:$/, '');
  } catch {
    throw createRuntimeError('invalid_argument', `'${url}' is not a valid URL`);
  }

  await ensureWotReady();
  const servient = getServient();
  if (!servient) {
    throw createRuntimeError('failed_precondition', 'The WoT servient is not running');
  }

  let client: any;
  try {
    client = servient.getClientFor(scheme);
  } catch {
    const supported = (servient.getClientSchemes?.() ?? []).join(', ');
    throw createRuntimeError(
      'invalid_argument',
      `No protocol binding handles scheme '${scheme}'. Supported schemes: ${supported}.`,
    );
  }

  await client.start();
  const content = await client.requestThingDescription(url);
  const body = await content.toBuffer();

  try {
    return { document: JSON.parse(body.toString('utf-8')) };
  } catch {
    throw createRuntimeError('unknown', `Describing '${url}' returned a document that is not valid JSON`);
  }
}

/**
 * Handles a health check request, returning the status of various runtime components.
 */
export async function handleGetRuntimeHealth(): Promise<any> {
  const health = await getRuntimeHealth();

  return {
    status: health.status,
    servientReady: health.servientReady,
    backendReachable: health.backendReachable,
    valkeyConfigured: health.valkeyConfigured,
    protocols: health.protocols,
    startedAt: health.startedAt || '',
  };
}
