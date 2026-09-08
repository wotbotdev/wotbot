/**
 * Detect node-wot DataSchemaError by duck-typing.
 *
 * The class sets its prototype to NotSupportedError (a known node-wot bug),
 * so `instanceof DataSchemaError` is unreliable.  We check the message and
 * the presence of a `.value` property instead.
 *
 * @param error The caught error object.
 * @returns True if the error is a node-wot DataSchemaError.
 */
export function isDataSchemaError(error: unknown): error is Error & { value: unknown } {
  return error instanceof Error && error.message === 'Invalid value according to DataSchema' && 'value' in error;
}

/**
 * Standard error codes for wot_runtime interactions.
 */
export type RuntimeErrorCode =
  | 'invalid_argument'
  | 'invalid_response'
  | 'not_found'
  | 'failed_precondition'
  | 'deadline_exceeded'
  | 'unimplemented'
  | 'permission_denied'
  | 'unauthenticated'
  | 'already_exists'
  | 'resource_exhausted'
  | 'internal'
  | 'unknown';

const HTTP_STATUS_BY_RUNTIME_ERROR_CODE: Record<RuntimeErrorCode, number> = {
  invalid_argument: 400,
  invalid_response: 502,
  not_found: 404,
  failed_precondition: 412,
  deadline_exceeded: 504,
  unimplemented: 501,
  permission_denied: 403,
  unauthenticated: 401,
  already_exists: 409,
  resource_exhausted: 429,
  internal: 500,
  unknown: 500,
};

/**
 * Specialized error class for wot_runtime logic and interactions.
 */
export class RuntimeError extends Error {
  code: RuntimeErrorCode;
  status: number;
  details: string;

  /**
   * Creates an instance of RuntimeError.
   *
   * @param code The standardized runtime error code.
   * @param message A human-readable error message.
   */
  constructor(code: RuntimeErrorCode, message: string) {
    super(message);
    this.name = 'RuntimeError';
    this.code = code;
    this.status = HTTP_STATUS_BY_RUNTIME_ERROR_CODE[code];
    this.details = message;
  }
}

/**
 * Checks if a value is a record object.
 *
 * @param value The value to check.
 * @returns True if the value is a non-null object and not an array.
 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

/**
 * Factory for creating RuntimeError instances.
 *
 * @param code The standardized runtime error code.
 * @param message A human-readable error message.
 * @returns A new RuntimeError instance.
 */
export function createRuntimeError(code: RuntimeErrorCode, message: string): RuntimeError {
  return new RuntimeError(code, message);
}

/**
 * Checks if an error is a RuntimeError.
 */
export function isRuntimeError(error: unknown): error is RuntimeError {
  return error instanceof RuntimeError;
}

/**
 * Extracts the HTTP status code from an error.
 * Defaults to 500 if the error is not a recognized RuntimeError.
 */
export function getRuntimeErrorStatus(error: unknown): number {
  return isRuntimeError(error) ? error.status : 500;
}

/**
 * Extracts the standardized error code from an error.
 */
export function getRuntimeErrorCode(error: unknown): RuntimeErrorCode | 'unknown' {
  return isRuntimeError(error) ? error.code : 'unknown';
}

/**
 * Formats an unknown error into a human-readable string.
 * Supports Error objects, strings, and plain object envelopes.
 *
 * @param error The caught error object.
 * @returns A human-readable error string.
 */
export function formatError(error: unknown): string {
  if (error instanceof Error) {
    return error.message || error.name || 'Unknown error';
  }

  if (typeof error === 'string') {
    return error;
  }

  if (isRecord(error)) {
    const details = typeof error.details === 'string' && error.details.trim() ? error.details.trim() : null;
    const message = typeof error.message === 'string' && error.message.trim() ? error.message.trim() : null;
    const name = typeof error.name === 'string' && error.name.trim() ? error.name.trim() : null;
    const code = typeof error.code === 'number' || typeof error.code === 'string' ? String(error.code) : null;
    const base = details || message;

    if (base) {
      const prefix = name && name !== base ? `${name}: ` : '';
      const suffix = code ? ` (code ${code})` : '';
      return `${prefix}${base}${suffix}`;
    }

    try {
      return JSON.stringify(error);
    } catch {
      return 'Unknown error';
    }
  }

  return String(error);
}
