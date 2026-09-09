import { Router } from 'express';

import {
  handleDescribeEndpoint,
  handleGetRuntimeHealth,
  handleInvokeAction,
  handleReadProperty,
  handleWriteProperty,
} from '../runtime/operations.js';
import { formatError, getRuntimeErrorCode, getRuntimeErrorStatus } from '../services/errors.js';
import { decodePayloadEnvelope, encodePayloadEnvelope, normalizeBody } from '../services/payloads.js';
import {
  ensureEventSubscription,
  ensurePropertyObservation,
  removeSubscription,
  subscriptionStatus,
} from '../services/subscriptions.js';

type JsonRecord = Record<string, unknown>;

/**
 * Checks if a value is a plain object.
 */
function isPlainObject(value: unknown): value is JsonRecord {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

/**
 * Extracts a string field from a request body, supporting both camelCase and snake_case keys.
 */
function getStringField(body: JsonRecord, camelKey: string, snakeKey: string): string {
  const direct = body[camelKey];
  if (typeof direct === 'string' && direct.trim()) {
    return direct.trim();
  }

  const snake = body[snakeKey];
  if (typeof snake === 'string' && snake.trim()) {
    return snake.trim();
  }

  return '';
}

/**
 * Extracts an optional string field from a request body.
 */
function getOptionalStringField(body: JsonRecord, camelKey: string, snakeKey: string): string | undefined {
  const value = getStringField(body, camelKey, snakeKey);
  return value || undefined;
}

/**
 * Extracts an optional integer field from a request body, supporting both camelCase and snake_case keys.
 */
function getOptionalIntegerField(body: JsonRecord, camelKey: string, snakeKey: string): number | undefined {
  const candidates = [body[camelKey], body[snakeKey]];
  for (const value of candidates) {
    if (typeof value === 'number' && Number.isInteger(value)) {
      return value;
    }
  }
  return undefined;
}

/**
 * Builds a WoT form selector object from request body fields.
 */
function buildFormSelector(body: JsonRecord): JsonRecord | undefined {
  const formSelector: JsonRecord = {};
  const formIndex = getOptionalIntegerField(body, 'formIndex', 'form_index');

  if (formIndex !== undefined) {
    formSelector.formIndex = formIndex;
  }

  const href = getOptionalStringField(body, 'href', 'href');
  const preferredScheme = getOptionalStringField(body, 'preferredScheme', 'preferred_scheme');
  const preferredContentType = getOptionalStringField(body, 'preferredContentType', 'preferred_content_type');
  const preferredSubprotocol = getOptionalStringField(body, 'preferredSubprotocol', 'preferred_subprotocol');

  if (href) {
    formSelector.href = href;
  }
  if (preferredScheme) {
    formSelector.preferredScheme = preferredScheme;
  }
  if (preferredContentType) {
    formSelector.preferredContentType = preferredContentType;
  }
  if (preferredSubprotocol) {
    formSelector.preferredSubprotocol = preferredSubprotocol;
  }

  return Object.keys(formSelector).length > 0 ? formSelector : undefined;
}

/**
 * Builds WoT URI variables from request body fields.
 */
function buildUriVariables(body: JsonRecord): JsonRecord[] | undefined {
  const raw = body.uriVariables ?? body.uri_variables;
  if (!isPlainObject(raw)) {
    return undefined;
  }

  const entries = Object.entries(raw).map(([name, value]) => ({
    name,
    value: encodePayloadEnvelope(value),
  }));

  return entries.length > 0 ? entries : undefined;
}

/**
 * Builds a WoT payload envelope from inline or base64-encoded request fields.
 */
function buildPayloadEnvelope(
  body: JsonRecord,
  valueKey: string,
  base64Key: string,
  contentTypeKey: string,
  snakeValueKey: string,
  snakeBase64Key: string,
  snakeContentTypeKey: string,
  options?: { allowNull?: boolean },
): JsonRecord | undefined {
  const contentType = getOptionalStringField(body, contentTypeKey, snakeContentTypeKey) || undefined;

  const base64Value = getOptionalStringField(body, base64Key, snakeBase64Key);
  if (base64Value) {
    return encodePayloadEnvelope(Buffer.from(base64Value, 'base64'), contentType);
  }

  const value = body[valueKey] ?? body[snakeValueKey];
  if (value === undefined) {
    return undefined;
  }
  if (value === null && options?.allowNull !== true) {
    return undefined;
  }

  return encodePayloadEnvelope(value, contentType);
}

/**
 * Builds a standardized WoT interaction target object.
 */
function buildTarget(thingId: string, affordanceName: string, operation: string): JsonRecord {
  return {
    thingId,
    affordanceName,
    operation,
  };
}

/**
 * Serializes a WoT payload envelope for inclusion in an HTTP response.
 * Handles inline values, binary data (base64), and content references.
 */
function serializePayloadEnvelope(payload: any): JsonRecord {
  const contentType = String(payload?.contentType || 'application/json');
  const body = normalizeBody(payload?.body);
  const decoded = decodePayloadEnvelope(payload);

  if (Buffer.isBuffer(decoded)) {
    return {
      kind: 'binary',
      content_type: contentType,
      size_bytes: body.length,
      body_base64: body.toString('base64'),
    };
  }

  return {
    kind: 'inline',
    content_type: contentType,
    size_bytes: body.length,
    data: decoded,
  };
}

/**
 * Serializes a node-wot interaction result into a standardized HTTP response structure.
 */
function serializeInteractionResponse(result: any): JsonRecord {
  const response = result?.response || result;
  return {
    success: Boolean(response?.success),
    status_code: Number(response?.statusCode || 0),
    status_text: String(response?.statusText || ''),
    response_content_type: String(response?.responseContentType || ''),
    matched_additional_response: Boolean(response?.matchedAdditionalResponse),
    chosen_form: response?.chosenForm || {},
    payload: serializePayloadEnvelope(response?.payload),
  };
}

/**
 * Helper to send a standardized error response.
 */
function sendError(response: any, error: unknown): void {
  response.status(getRuntimeErrorStatus(error)).json({
    detail: formatError(error),
    error_code: getRuntimeErrorCode(error),
  });
}

/**
 * Creates the Express router for wot_runtime's internal API.
 * This API is used by the search indexer and other registry components to interact with WoT devices.
 */
export function createRuntimeRouter(): Router {
  const router = Router();

  router.get('/health', async (_request, response) => {
    try {
      response.json(await handleGetRuntimeHealth());
    } catch (error) {
      sendError(response, error);
    }
  });

  router.post('/describe-endpoint', async (request, response) => {
    const body = isPlainObject(request.body) ? request.body : {};
    try {
      const url = getStringField(body, 'url', 'url');
      const result = await handleDescribeEndpoint({ url });

      response.json({ url, document: result.document });
    } catch (error) {
      sendError(response, error);
    }
  });

  router.post('/read-property', async (request, response) => {
    const body = isPlainObject(request.body) ? request.body : {};
    try {
      const thingId = getStringField(body, 'thingId', 'thing_id');
      const propertyName =
        getStringField(body, 'propertyName', 'property_name') ||
        getStringField(body, 'affordanceName', 'affordance_name');

      const result = await handleReadProperty({
        target: buildTarget(thingId, propertyName, 'OPERATION_TYPE_READ_PROPERTY'),
        uriVariables: buildUriVariables(body),
        formSelector: buildFormSelector(body),
      });

      response.json({
        thing_id: thingId,
        property_name: propertyName,
        result: serializeInteractionResponse(result),
      });
    } catch (error) {
      sendError(response, error);
    }
  });

  router.post('/write-property', async (request, response) => {
    const body = isPlainObject(request.body) ? request.body : {};
    try {
      const thingId = getStringField(body, 'thingId', 'thing_id');
      const propertyName =
        getStringField(body, 'propertyName', 'property_name') ||
        getStringField(body, 'affordanceName', 'affordance_name');

      const result = await handleWriteProperty({
        target: buildTarget(thingId, propertyName, 'OPERATION_TYPE_WRITE_PROPERTY'),
        input: buildPayloadEnvelope(
          body,
          'value',
          'valueBase64',
          'valueContentType',
          'value',
          'value_base64',
          'value_content_type',
          { allowNull: true },
        ),
        uriVariables: buildUriVariables(body),
        formSelector: buildFormSelector(body),
      });

      response.json({
        thing_id: thingId,
        property_name: propertyName,
        result: serializeInteractionResponse(result),
      });
    } catch (error) {
      sendError(response, error);
    }
  });

  router.post('/invoke-action', async (request, response) => {
    const body = isPlainObject(request.body) ? request.body : {};
    try {
      const thingId = getStringField(body, 'thingId', 'thing_id');
      const actionName =
        getStringField(body, 'actionName', 'action_name') || getStringField(body, 'affordanceName', 'affordance_name');

      const result = await handleInvokeAction({
        target: buildTarget(thingId, actionName, 'OPERATION_TYPE_INVOKE_ACTION'),
        input: buildPayloadEnvelope(
          body,
          'input',
          'inputBase64',
          'inputContentType',
          'input',
          'input_base64',
          'input_content_type',
        ),
        uriVariables: buildUriVariables(body),
        formSelector: buildFormSelector(body),
        idempotencyKey: getOptionalStringField(body, 'idempotencyKey', 'idempotency_key') || '',
      });

      if (result?.completedResult) {
        response.json({
          thing_id: thingId,
          action_name: actionName,
          outcome: 'completed_result',
          completed_result: serializeInteractionResponse(result.completedResult),
        });
        return;
      }

      response.json({
        thing_id: thingId,
        action_name: actionName,
        outcome: 'operation_handle',
        operation_handle: result.operationHandle,
      });
    } catch (error) {
      sendError(response, error);
    }
  });

  router.post('/observe-property', async (request, response) => {
    const body = isPlainObject(request.body) ? request.body : {};
    try {
      const thingId = getStringField(body, 'thingId', 'thing_id');
      const propertyName =
        getStringField(body, 'propertyName', 'property_name') ||
        getStringField(body, 'affordanceName', 'affordance_name');

      response.json(
        await ensurePropertyObservation({
          subscriptionNamespace: getStringField(body, 'subscriptionNamespace', 'subscription_namespace'),
          target: buildTarget(thingId, propertyName, 'OPERATION_TYPE_OBSERVE_PROPERTY'),
          uriVariables: buildUriVariables(body),
          formSelector: buildFormSelector(body),
        }),
      );
    } catch (error) {
      sendError(response, error);
    }
  });

  router.post('/subscribe-event', async (request, response) => {
    const body = isPlainObject(request.body) ? request.body : {};
    try {
      const thingId = getStringField(body, 'thingId', 'thing_id');
      const eventName =
        getStringField(body, 'eventName', 'event_name') || getStringField(body, 'affordanceName', 'affordance_name');

      response.json(
        await ensureEventSubscription({
          subscriptionNamespace: getStringField(body, 'subscriptionNamespace', 'subscription_namespace'),
          target: buildTarget(thingId, eventName, 'OPERATION_TYPE_SUBSCRIBE_EVENT'),
          uriVariables: buildUriVariables(body),
          subscriptionInput: buildPayloadEnvelope(
            body,
            'subscriptionInput',
            'subscriptionInputBase64',
            'subscriptionInputContentType',
            'subscription_input',
            'subscription_input_base64',
            'subscription_input_content_type',
          ),
          formSelector: buildFormSelector(body),
        }),
      );
    } catch (error) {
      sendError(response, error);
    }
  });

  router.post('/subscription-status', (request, response) => {
    const body = isPlainObject(request.body) ? request.body : {};
    response.json(subscriptionStatus(getStringField(body, 'subscriptionId', 'subscription_id')));
  });

  router.post('/remove-subscription', async (request, response) => {
    const body = isPlainObject(request.body) ? request.body : {};
    try {
      response.json(
        await removeSubscription({
          subscriptionId: getStringField(body, 'subscriptionId', 'subscription_id'),
          cancellationInput: buildPayloadEnvelope(
            body,
            'cancellationInput',
            'cancellationInputBase64',
            'cancellationInputContentType',
            'cancellation_input',
            'cancellation_input_base64',
            'cancellation_input_content_type',
          ),
        }),
      );
    } catch (error) {
      sendError(response, error);
    }
  });

  return router;
}
