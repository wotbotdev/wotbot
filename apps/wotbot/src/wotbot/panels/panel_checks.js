/**
 * window.panelChecks — declared checks a generated panel runs against itself.
 *
 * Static validation cannot tell whether a rendered panel agrees with the data it
 * was built from, and neither can executing it and watching for exceptions: a
 * panel that states "the ranking is identical at all three heights" renders
 * perfectly whether or not that is true. A check expressed here is answerable,
 * because `window.panelData` carries the same attachment the claim came from.
 *
 * Usage, once, after the panel has finished rendering:
 *
 *   const verdict = await panelChecks({
 *     'map rendered': () => map.getLayers().length > 0,
 *     'ranking identical at 80m and 100m': () => {
 *       const wind = panelData.read('wind');
 *       return rankAt(wind, 80).join() === rankAt(wind, 100).join();
 *     },
 *   });
 *
 * A check passes by returning a value that is not false, null, undefined or
 * NaN. Returning nothing therefore fails rather than passing silently, because
 * a check that cannot fail is worse than no check at all. Throwing fails and
 * carries the message. Checks may be async and run in declaration order.
 *
 * The settled verdict is exposed as `window.panelChecksResult` and posted to the
 * host frame, so a validator can read it either by evaluating the global or by
 * listening for the message:
 *
 *   { status: 'passed' | 'failed' | 'empty',
 *     checks: [ { label, passed, error } ] }
 *
 * 'empty' means the panel declared no checks. It is deliberately not 'passed':
 * an absent check is unknown, not satisfied.
 *
 * This file is the single canonical source, inlined into every generated panel
 * document by `wrap_panel_document` (panels/render.py), keeping the helpers
 * available on the panel's isolated origin without a separate script endpoint.
 */
(() => {
  'use strict';

  const RESULT_SOURCE = 'panel-checks';
  let called = false;
  let verdict = null;

  // Reserve the result before authored code runs, including while async checks
  // are pending. Only this closure can publish a settled verdict.
  Object.defineProperty(window, 'panelChecksResult', { get: () => verdict });

  const describe = (error) =>
    error instanceof Error ? error.message || String(error) : String(error);

  // Only these count as failure, so a check returning 0 or '' still passes;
  // those are ordinary values, whereas these four mean "produced no answer".
  const isBlank = (outcome) =>
    outcome === false ||
    outcome === null ||
    outcome === undefined ||
    (typeof outcome === 'number' && Number.isNaN(outcome));

  async function evaluate(label, check) {
    if (typeof check !== 'function') {
      return { label, passed: false, error: 'check is not a function' };
    }
    try {
      const outcome = await check();
      if (isBlank(outcome)) {
        return { label, passed: false, error: 'check returned ' + String(outcome) };
      }
      return { label, passed: true, error: null };
    } catch (error) {
      return { label, passed: false, error: describe(error) };
    }
  }

  async function panelChecks(checks) {
    if (called) {
      // The verdict is posted once and frozen, so a second set of checks would
      // be silently discarded. Fail loudly instead of reporting a partial pass.
      throw new Error('panelChecks was already called; declare every check in one call');
    }
    called = true;

    const results = [];
    for (const [label, check] of Object.entries(checks || {})) {
      results.push(await evaluate(label, check));
    }

    let status = 'passed';
    if (results.length === 0) status = 'empty';
    else if (results.some((result) => !result.passed)) status = 'failed';

    verdict = Object.freeze({
      status,
      checks: Object.freeze(results.map((result) => Object.freeze(result))),
    });

    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage({ source: RESULT_SOURCE, verdict }, '*');
      }
    } catch {
      // A panel opened directly has no host; the global still carries the verdict.
    }

    return verdict;
  }

  Object.defineProperty(window, 'panelChecks', { value: panelChecks });
})();
