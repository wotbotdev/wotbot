import crypto from 'node:crypto';

import { config } from '../config/env.js';
import log from '../logger/index.js';
import { formatError } from './errors.js';
import { getValkeyClient } from './valkey-client.js';

/**
 * Structure stored in the cache for each response.
 */
interface CachedResponse {
  contentType: string;
  payload: string; // base64-encoded
  statusCode: number;
}

/**
 * Builds a deterministic cache key from interaction parameters.
 */
export function buildCacheKey(
  thingId: string,
  operation: string,
  affordanceName: string,
  uriVariables?: Record<string, unknown>,
  input?: unknown,
  formIndex?: number,
): string {
  const params = JSON.stringify({ u: uriVariables ?? null, i: input ?? null, f: formIndex }, (_key, value) =>
    isPlainObject(value) ? sortObject(value) : value,
  );
  const hash = crypto.createHash('sha256').update(params).digest('hex').slice(0, 16);
  return `wot:cache:${thingId}:${operation}:${affordanceName}:${hash}`;
}

/**
 * Retrieves a cached response, or null if not found or caching is disabled.
 */
export async function getCached(key: string): Promise<CachedResponse | null> {
  if (!config.cacheEnabled) return null;

  try {
    const client = await getValkeyClient();
    const raw = await client.get(key);
    if (!raw) return null;

    log.debug(`Cache hit: ${key}`);
    return JSON.parse(raw) as CachedResponse;
  } catch (error) {
    log.warn(`Cache read error: ${formatError(error)}`);
    return null;
  }
}

/**
 * Stores a response in the cache with the configured TTL.
 * Silently skips if caching is disabled or the payload exceeds the max size.
 */
export async function setCached(key: string, response: CachedResponse, payloadSizeBytes: number): Promise<void> {
  if (!config.cacheEnabled) return;
  if (payloadSizeBytes > config.cacheMaxBytes) {
    log.debug(`Cache skip (too large): ${key} (${payloadSizeBytes} bytes)`);
    return;
  }

  try {
    const client = await getValkeyClient();
    await client.set(key, JSON.stringify(response), 'EX', config.cacheTtlSeconds);
    log.debug(`Cache set: ${key} (ttl=${config.cacheTtlSeconds}s, size=${payloadSizeBytes})`);
  } catch (error) {
    log.warn(`Cache write error: ${formatError(error)}`);
  }
}

/**
 * Drops a cached entry, so a response that later proves unusable does not keep
 * being served for the rest of its TTL.
 */
export async function deleteCached(key: string): Promise<void> {
  if (!config.cacheEnabled) return;

  try {
    const client = await getValkeyClient();
    await client.del(key);
    log.debug(`Cache delete: ${key}`);
  } catch (error) {
    log.warn(`Cache delete error: ${formatError(error)}`);
  }
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function sortObject(obj: Record<string, unknown>): Record<string, unknown> {
  const sorted: Record<string, unknown> = {};
  for (const key of Object.keys(obj).sort()) {
    sorted[key] = obj[key];
  }
  return sorted;
}
