import { createRuntimeError, isDataSchemaError } from './errors.js';

/**
 * Checks if a value is a plain object (not null, not an array).
 */
function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

/**
 * Normalizes a potential body value into a Buffer.
 * Handles Buffers, Uint8Arrays, and number arrays.
 * Returns an empty Buffer for other types.
 *
 * @param body The raw body to normalize.
 * @returns A Buffer representation of the body.
 */
export function normalizeBody(body: unknown): Buffer {
  if (Buffer.isBuffer(body)) {
    return body;
  }

  if (body instanceof Uint8Array) {
    return Buffer.from(body);
  }

  if (Array.isArray(body)) {
    return Buffer.from(body);
  }

  return Buffer.alloc(0);
}

/**
 * Decodes a payload envelope into its high-level representation.
 * Automatically handles JSON parsing for application/json or unknown content types.
 * Returns a string for text/* types.
 * Returns a Buffer for other types.
 *
 * @param payload The payload envelope to decode (usually { body: Buffer, contentType: string }).
 * @returns The decoded value (object, string, or Buffer).
 */
export function decodePayloadEnvelope(payload: any): unknown {
  if (!payload) {
    return undefined;
  }

  const body = normalizeBody(payload.body);
  if (body.length === 0) {
    return undefined;
  }

  const contentType = String(payload.contentType || '')
    .trim()
    .toLowerCase();
  const text = body.toString('utf8');

  if (!contentType || contentType.includes('json')) {
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }

  if (contentType.startsWith('text/')) {
    return text;
  }

  return body;
}

/**
 * Encodes a high-level value into a payload envelope.
 * Handles undefined, Buffers, Uint8Arrays, strings, and JSON-serializable objects.
 *
 * @param value The value to encode.
 * @param contentType Optional target content type. Defaults to application/json or text/plain.
 * @returns A payload envelope object { body: Buffer, contentType: string }.
 */
export function encodePayloadEnvelope(value: unknown, contentType?: string): any {
  if (value === undefined) {
    return {
      body: Buffer.alloc(0),
      contentType: contentType || 'application/json',
    };
  }

  if (Buffer.isBuffer(value) || value instanceof Uint8Array) {
    return {
      body: Buffer.from(value),
      contentType: contentType || 'application/octet-stream',
    };
  }

  if (typeof value === 'string') {
    return {
      body: Buffer.from(value, 'utf8'),
      contentType: contentType || 'text/plain; charset=utf-8',
    };
  }

  return {
    body: Buffer.from(JSON.stringify(value), 'utf8'),
    contentType: contentType || 'application/json',
  };
}

/**
 * Extracts the content type from a WoT Form object.
 * Checks for response-level content type first, then top-level form content type.
 */
function extractContentType(form: unknown): string {
  if (!isPlainObject(form)) {
    return 'application/json';
  }

  const response = form.response;
  if (isPlainObject(response) && typeof response.contentType === 'string') {
    return response.contentType;
  }

  if (typeof form.contentType === 'string' && form.contentType.trim()) {
    return form.contentType;
  }

  return 'application/json';
}

/**
 * Extracts the protocol (e.g., 'http', 'coap') from a URI string.
 *
 * @param href The URI string.
 * @returns The protocol name or an empty string if invalid.
 */
export function extractProtocol(href: unknown): string {
  if (typeof href !== 'string' || href.trim().length === 0) {
    return '';
  }

  try {
    return new URL(href).protocol.replace(/:$/, '');
  } catch {
    return '';
  }
}

function isJsonContentType(contentType: string): boolean {
  const mediaType = contentType.split(';', 1)[0].trim().toLowerCase();
  return mediaType === 'application/json' || mediaType === 'text/json' || mediaType.endsWith('+json');
}

/**
 * Reject HTML error pages masquerading as JSON or tabular data.
 * Inspect original bytes so JSON strings containing markup remain valid data.
 * HTML, XML, plain text and unknown binary formats have no such restriction.
 */
export function rejectHtmlPayload(body: Buffer, contentType: string): void {
  const mediaType = contentType.split(';', 1)[0].trim().toLowerCase();
  const json = isJsonContentType(contentType);
  if (!json && !['text/csv', 'application/csv', 'text/tab-separated-values'].includes(mediaType)) {
    return;
  }

  const head = body.subarray(0, 512).toString('utf8').trimStart();
  const document = /^(?:<!doctype\s+html(?:\s|>)|<(?:html|head|body)(?:\s|>))/i.test(head);
  const fragment = json && /^<(?:h[1-6]|p|div)(?:\s|>)/i.test(head);
  if (document || fragment) {
    throw createRuntimeError(
      'invalid_response',
      `The upstream returned HTML while the Thing declares '${mediaType}'. ` +
        'Check the source URL and the action URI variables.',
    );
  }
}

/**
 * Encodes the output of a WoT interaction (property read or action invocation) into a standardized payload.
 * Handles schema validation errors by returning the invalid value if possible.
 * Fallbacks to raw array buffer if high-level value extraction fails.
 *
 * @param output The InteractionOutput from node-wot.
 * @param options Optional callbacks for handling specific events (e.g., schema validation failure).
 * @returns A promise resolving to an object containing the body Buffer, content type, and source protocol.
 */
export async function encodeInteractionOutputPayload(
  output: any,
  options?: {
    onInvalidSchema?: (value: unknown) => void;
  },
): Promise<{
  body: Buffer;
  contentType: string;
  sourceProtocol: string;
}> {
  const form = output?.form;
  const contentType = extractContentType(form);
  const sourceProtocol = extractProtocol(isPlainObject(form) ? form.href : '');
  const readRawBody = async () => {
    const body = Buffer.from(await output.arrayBuffer());
    rejectHtmlPayload(body, contentType);
    return body;
  };
  const rawOutput = async () => ({ body: await readRawBody(), contentType, sourceProtocol });

  if (output?.schema == null && typeof output?.arrayBuffer === 'function') {
    return rawOutput();
  }

  let value: unknown;
  try {
    value = await output.value();
  } catch (error) {
    if (isDataSchemaError(error)) {
      options?.onInvalidSchema?.(error.value);
      value = error.value;
    } else {
      return rawOutput();
    }
  }

  // node-wot retains the original buffer after value(), including schema errors.
  // Validate outside the catch so a rejected page cannot fall back to success.
  await readRawBody();
  const payload = encodePayloadEnvelope(value, contentType);
  return {
    body:
      typeof value === 'string' && isJsonContentType(contentType)
        ? Buffer.from(JSON.stringify(value))
        : normalizeBody(payload.body),
    contentType: String(payload.contentType || contentType),
    sourceProtocol,
  };
}
