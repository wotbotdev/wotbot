import type { ThingDescription } from './thing-catalog-client.js';

type JsonRecord = Record<string, unknown>;

/**
 * Valid operation names for WoT affordances.
 */
export type AffordanceOperation =
  | 'readproperty'
  | 'writeproperty'
  | 'observeproperty'
  | 'invokeaction'
  | 'subscribeevent';

/**
 * Normalized representation of a form selector.
 */
type NormalizedFormSelector = {
  formIndex?: number;
  href?: string;
  preferredScheme?: string;
  preferredContentType?: string;
  preferredSubprotocol?: string;
};

/**
 * Checks if a value is a plain object.
 */
function isPlainObject(value: unknown): value is JsonRecord {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

/**
 * Cleans and trims a string value.
 */
function cleanString(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

/**
 * Normalizes a media type string (e.g., "application/json; charset=utf-8" -> "application/json").
 */
function normalizeMediaType(value: unknown): string {
  const raw = cleanString(value).toLowerCase();
  return raw.split(';', 1)[0] || '';
}

/**
 * Normalizes a string or array of strings into a lowercase array of strings.
 */
function normalizeStringArray(value: unknown): string[] {
  if (typeof value === 'string') {
    const cleaned = cleanString(value).toLowerCase();
    return cleaned ? [cleaned] : [];
  }

  if (!Array.isArray(value)) {
    return [];
  }

  return value.map((item) => cleanString(item).toLowerCase()).filter((item) => item.length > 0);
}

/**
 * Extracts the URI scheme from an href string.
 */
function extractScheme(href: string): string {
  try {
    return new URL(href).protocol.replace(/:$/, '').toLowerCase();
  } catch {
    const match = href.match(/^([a-z][a-z0-9+.-]*):/i);
    return match ? match[1].toLowerCase() : '';
  }
}

/**
 * Normalizes an untrusted form selector object.
 */
function normalizeFormSelector(formSelector: unknown): NormalizedFormSelector | null {
  if (!isPlainObject(formSelector)) {
    return null;
  }

  const formIndex = formSelector.formIndex;
  const normalized: NormalizedFormSelector = {};

  if (typeof formIndex === 'number' && Number.isInteger(formIndex) && formIndex >= 0) {
    normalized.formIndex = formIndex;
  }

  const href = cleanString(formSelector.href);
  if (href) {
    normalized.href = href;
  }

  const preferredScheme = cleanString(formSelector.preferredScheme).toLowerCase();
  if (preferredScheme) {
    normalized.preferredScheme = preferredScheme;
  }

  const preferredContentType = normalizeMediaType(formSelector.preferredContentType);
  if (preferredContentType) {
    normalized.preferredContentType = preferredContentType;
  }

  const preferredSubprotocol = cleanString(formSelector.preferredSubprotocol).toLowerCase();
  if (preferredSubprotocol) {
    normalized.preferredSubprotocol = preferredSubprotocol;
  }

  return Object.keys(normalized).length > 0 ? normalized : null;
}

/**
 * Maps an AffordanceOperation to its corresponding section in a Thing Description.
 */
function operationSection(operation: AffordanceOperation): 'properties' | 'actions' | 'events' {
  if (operation === 'invokeaction') {
    return 'actions';
  }
  if (operation === 'subscribeevent') {
    return 'events';
  }
  return 'properties';
}

/**
 * Safely extracts the Thing ID from a Thing Description.
 */
function thingIdFromDocument(document: ThingDescription): string {
  const thingId = cleanString(document.id);
  return thingId || 'unknown';
}

/**
 * Retrieves the definition of a specific affordance from a Thing Description.
 *
 * @param document The Thing Description.
 * @param affordanceName The name of the affordance (property, action, or event).
 * @param operation The operation being performed.
 * @returns The affordance definition object or null if not found.
 */
export function getAffordanceDefinition(
  document: ThingDescription,
  affordanceName: string,
  operation: AffordanceOperation,
): JsonRecord | null {
  const section = document[operationSection(operation)];
  if (!isPlainObject(section)) {
    return null;
  }

  const affordance = section[affordanceName];
  return isPlainObject(affordance) ? affordance : null;
}

/**
 * Retrieves the forms array for a specific affordance.
 */
function getAffordanceForms(
  document: ThingDescription,
  affordanceName: string,
  operation: AffordanceOperation,
): JsonRecord[] {
  const affordance = getAffordanceDefinition(document, affordanceName, operation);
  if (!affordance || !Array.isArray(affordance.forms)) {
    return [];
  }

  return affordance.forms.filter(isPlainObject);
}

/**
 * Checks if a specific form supports the requested operation.
 */
function formSupportsOperation(form: JsonRecord, operation: AffordanceOperation): boolean {
  const operations = normalizeStringArray(form.op);
  return operations.length === 0 || operations.includes(operation);
}

/**
 * Returns the uppercased HTTP method (htv:methodName) of the form that will be
 * used for an HTTP or provider interaction, or undefined for other bindings.
 *
 * When no explicit form index is resolved, node-wot selects the first form that
 * supports the operation, so we mirror that choice here.
 */
export function getFormHttpMethod(
  document: ThingDescription,
  affordanceName: string,
  operation: AffordanceOperation,
  formIndex: number | undefined,
): string | undefined {
  const forms = getAffordanceForms(document, affordanceName, operation);
  const form =
    formIndex !== undefined ? forms[formIndex] : forms.find((candidate) => formSupportsOperation(candidate, operation));
  if (!isPlainObject(form)) {
    return undefined;
  }

  const scheme = extractScheme(cleanString(form.href)) || extractScheme(cleanString(document.base));
  if (!['http', 'https', 'wotbot+provider'].includes(scheme)) {
    return undefined;
  }

  const method = cleanString(form['htv:methodName']);
  return method ? method.toUpperCase() : undefined;
}

/**
 * Checks if a form matches the criteria in a normalized form selector.
 */
function formMatchesSelector(
  form: JsonRecord,
  formIndex: number,
  selector: NormalizedFormSelector,
  operation: AffordanceOperation,
): boolean {
  if (selector.formIndex !== undefined && selector.formIndex !== formIndex) {
    return false;
  }

  if (!formSupportsOperation(form, operation)) {
    return false;
  }

  if (selector.href && cleanString(form.href) !== selector.href) {
    return false;
  }

  if (selector.preferredScheme) {
    const scheme = extractScheme(cleanString(form.href));
    if (scheme !== selector.preferredScheme) {
      return false;
    }
  }

  if (selector.preferredContentType) {
    const contentType = normalizeMediaType(form.contentType);
    if (contentType !== selector.preferredContentType) {
      return false;
    }
  }

  if (selector.preferredSubprotocol) {
    const subprotocols = normalizeStringArray(form.subprotocol);
    if (!subprotocols.includes(selector.preferredSubprotocol)) {
      return false;
    }
  }

  return true;
}

/**
 * Resolves a form index from a Thing Description based on a form selector.
 * Throws an error if a selector was provided but no form matched it.
 * Returns undefined if no selector was provided (letting node-wot choose the best form).
 *
 * @param document The Thing Description.
 * @param affordanceName The name of the affordance.
 * @param operation The operation being performed.
 * @param formSelector The untrusted form selector object from the request.
 * @returns The matched form index or undefined.
 * @throws {Error} if a selector was provided but no match was found.
 */
export function resolveFormIndex(
  document: ThingDescription,
  affordanceName: string,
  operation: AffordanceOperation,
  formSelector: unknown,
): number | undefined {
  const selector = normalizeFormSelector(formSelector);
  if (!selector) {
    return undefined;
  }

  const forms = getAffordanceForms(document, affordanceName, operation);
  const matchedIndex = forms.findIndex((form, index) => formMatchesSelector(form, index, selector, operation));

  if (matchedIndex >= 0) {
    return matchedIndex;
  }

  const thingId = thingIdFromDocument(document);
  throw new Error(
    `No form matched the requested selector for '${operation}' on Thing '${thingId}' affordance '${affordanceName}'`,
  );
}
