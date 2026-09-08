/**
 * Panels are served from an origin of their own, one per panel.
 *
 * A panel is LLM-authored code. Served from the app's origin it could only be
 * contained by a sandbox without `allow-same-origin`, which forces an opaque
 * origin -- and an opaque origin is denied every permission-gated API, so no
 * panel could ever use a camera, persist anything, or record audio.
 *
 * Giving each panel its own origin inverts that. `allow-same-origin` then means
 * "your own real origin", which is still cross-origin to the app and still
 * walled off from its DOM, storage and cookies by ordinary same-origin policy --
 * while the panel gets the whole platform back. Per panel rather than one shared
 * panel host, so a camera grant for one panel does not arm every later one.
 *
 * Locally this needs no configuration: Chrome resolves any `*.localhost` to
 * loopback and treats it as a secure context. In production it needs a wildcard
 * host, hence `PANEL_HOST_TEMPLATE`.
 *
 * That value is read at *runtime*, not build time. A `NEXT_PUBLIC_` variable is
 * inlined into the client bundle when the image is built, so setting one in a
 * deployment's `.env` would change nothing while looking like it had -- panels
 * would quietly fall back to a derived host with no wildcard DNS behind it. The
 * server therefore publishes it on the document (see `app/layout.tsx`) and the
 * browser reads it back from there.
 */

/** Placeholder replaced with the panel's own label. */
const KEY_TOKEN = '{key}';

/** Where the server publishes the template for the browser to read back. */
export const PANEL_HOST_TEMPLATE_ATTRIBUTE = 'data-panel-host-template';

function configuredTemplate(): string | undefined {
  if (typeof document !== 'undefined') {
    return document.documentElement.dataset.panelHostTemplate || undefined;
  }
  return process.env.PANEL_HOST_TEMPLATE || undefined;
}

/**
 * Reduce an id or filename to a single DNS label.
 *
 * Single, deliberately: a wildcard certificate for `*.panels.example.com`
 * matches one label only, so a dotted filename must not become `a.b.panels…`.
 */
export function toPanelLabel(key: string): string {
  // The suffix keeps the mapping injective. Normalisation is lossy --
  // `chart_1.html` and `chart-1.html` both reduce to `chart-1`, as does any pair
  // differing only in punctuation or beyond 63 characters -- and two panels
  // sharing a label would share an origin, and therefore one camera grant.
  const suffix = fingerprint(key);
  const base = key
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 63 - suffix.length - 1)
    .replace(/-+$/g, '');
  return base ? `${base}-${suffix}` : `panel-${suffix}`;
}

/** Short, stable, non-cryptographic digest; only collision resistance matters. */
function fingerprint(value: string): string {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

/**
 * The origin a given panel is served from.
 *
 * Returns an empty string during server rendering, where there is no location
 * to derive from; callers build a relative URL until hydration supplies one.
 */
export function getPanelOrigin(
  key: string,
  location:
    | { protocol: string; hostname: string; port: string }
    | undefined = typeof window === 'undefined' ? undefined : window.location,
  template: string | undefined = configuredTemplate(),
): string {
  if (!location) {
    return '';
  }

  const label = toPanelLabel(key);
  const port = location.port ? `:${location.port}` : '';

  if (isUsableTemplate(template)) {
    // No port appended: a template names a host of its own, which need not be
    // served on the port the app happens to be reached at.
    return `${location.protocol}//${template.replace(KEY_TOKEN, label)}`;
  }

  return `${location.protocol}//${label}.panels.${location.hostname}${port}`;
}

/** Whether a request arrived on a panel host rather than the app's own. */
export function isPanelHostname(
  hostname: string,
  template: string | undefined = configuredTemplate(),
): boolean {
  if (isUsableTemplate(template)) {
    // The proxy passes a hostname without its port. Templates may carry one for
    // getPanelOrigin; keeping it here would never match and bypass the guard.
    const suffix = template.slice(KEY_TOKEN.length).split(':')[0].toLowerCase();
    return hostname !== suffix && hostname.endsWith(suffix);
  }
  return /^[a-z0-9-]+\.panels\./.test(hostname);
}

/**
 * A template is only usable when `{key}` is its leading label.
 *
 * Anything else makes the suffix ambiguous: `panel-{key}.example.com` would
 * leave `.example.com`, so the app's own host matches too and `proxy.ts` would
 * 404 the entire application. Falling back to the derived host is safe and
 * visibly wrong, which is what an operator needs.
 */
function isUsableTemplate(template: string | undefined): template is string {
  return Boolean(template) && template!.startsWith(KEY_TOKEN);
}
