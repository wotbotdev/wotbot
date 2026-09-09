/** MCP transport for the existing generated panel window.wot contract. */
(function () {
  'use strict';
  let resolveReady, rejectReady;
  const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
  // No unhandled rejection when a panel never requests a Thing operation.
  ready.catch(() => {});
  let app, grant, closed = false;
  const callbacks = new Map();
  const objectUrls = new Set();
  const timer = setTimeout(() => rejectReady(new Error('MCP Apps host initialization or panel launch timed out')), 15000);

  window.__wotMcpConnect = async function (App) {
    delete window.__wotMcpConnect;
    try {
      app = new App({ name: 'WoTBot panel', version: '1.0.0' });
      let connected = false;
      const finish = () => { if (connected && grant) { clearTimeout(timer); resolveReady(); } };
      app.ontoolinput = () => {};
      app.ontoolresult = result => {
        const launch = result?._meta?.['dev.wotbot/panel-launch'];
        if (typeof launch?.grant === 'string') { grant = launch.grant; finish(); }
      };
      app.onteardown = async () => { await cleanup(); return {}; };
      await app.connect();
      connected = true;
      finish();
      window.dispatchEvent(new Event('mcp-app-ready'));
    } catch (error) { clearTimeout(timer); rejectReady(error); }
  };

  async function call(tool, args) {
    await ready;
    const response = await app.callServerTool({ name: 'panels.call', arguments: { grant, tool, arguments: args } });
    if (response.isError) {
      throw new Error(response.content?.find(p => p.type === 'text')?.text || 'Panel operation failed');
    }
    const data = response.structuredContent;
    const value = data && Object.prototype.hasOwnProperty.call(data, 'result')
      ? data.result : JSON.parse(response.content.find(p => p.type === 'text').text);
    if (value && typeof value === 'object' && value.error) throw new Error(String(value.error));
    return value;
  }
  const operations = {readProperty: 'things.read_property', writeProperty: 'things.write_property',
    invokeAction: 'things.invoke_action', observeProperty: 'things.observe_property', subscribeEvent: 'things.subscribe_event'};
  function options(opts) {
    const {uriVariables, formIndex, input, inputContentType, inputBase64} = opts || {};
    return {uriVariables, formIndex, input, inputContentType, inputBase64};
  }
  async function send(op, thingId, name, value, opts) {
    if (closed) throw new Error('This panel is closed');
    const key = op === 'invokeAction' ? 'actionName' : op === 'subscribeEvent' ? 'eventName' : 'propertyName';
    const args = {...options(opts), thingId, [key]: name};
    if (op === 'writeProperty' || op === 'invokeAction') {
      const field = op === 'writeProperty' ? 'value' : 'input';
      if (isBinaryPayload(value)) {
        args[field + 'Base64'] = value.bodyBase64;
        args[field + 'ContentType'] = value.contentType;
      } else if (value !== undefined) args[field] = value;
    }
    return call(operations[op], args);
  }
  function subscriptionId(result) {
    if (!result || typeof result !== 'object') return null;
    if (result.subscriptionId || result.subscription_id) return result.subscriptionId || result.subscription_id;
    for (const key of ['subscription', 'result', 'completed_result', 'payload', 'data']) {
      const id = subscriptionId(result[key]); if (id) return id;
    }
    return null;
  }
  // The panel keeps its own stream position, so an idle subscription costs
  // the server nothing but the blocking read.
  async function poll(id, cursor) {
    while (!closed && callbacks.has(id)) {
      try {
        const args = {subscriptionId: id, timeoutMs: 25000};
        if (cursor) args.cursor = cursor;
        const result = await call('things.next_subscription_event', args);
        if (result?.nextCursor) cursor = result.nextCursor;
        const callback = callbacks.get(id);
        if (callback && result?.event) {
          try { callback(result.event.value, result.event); } catch (_) { /* widget failure is local */ }
        }
      } catch (error) {
        callbacks.delete(id);
        window.dispatchEvent(new CustomEvent('wot-error', {detail: {subscriptionId: id, message: error.message}}));
        break;
      }
    }
  }
  async function subscribe(op, thingId, name, callback, opts) {
    if (typeof callback !== 'function') throw new Error('A subscription callback is required');
    const result = await send(op, thingId, name, undefined, opts);
    const id = subscriptionId(result);
    if (!id) throw new Error('The runtime did not return a subscription identifier');
    if (closed) { await call('things.unsubscribe', {subscriptionId: id}); return {subscriptionId: id}; }
    callbacks.set(id, callback);
    // Start from the position captured before the subscription was created,
    // so an event delivered in between is not skipped.
    void poll(id, result?.cursor);
    return {subscriptionId: id};
  }
  async function unsubscribe(handle) {
    handle = await handle;
    const id = handle?.subscriptionId || handle;
    if (!id) return;
    callbacks.delete(id);
    return call('things.unsubscribe', {subscriptionId: id});
  }
  async function cleanup() {
    closed = true;
    const ids = [...callbacks.keys()];
    callbacks.clear();
    await Promise.allSettled(ids.map(id => call('things.unsubscribe', {subscriptionId: id})));
    for (const url of objectUrls) URL.revokeObjectURL(url);
    objectUrls.clear();
  }
  function isBinaryPayload(value) {
    return (
      value &&
      typeof value === 'object' &&
      value.kind === 'binary' &&
      typeof value.bodyBase64 === 'string'
    );
  }

  function binaryToBytes(payload) {
    if (!isBinaryPayload(payload)) {
      throw new Error('Expected a binary payload from window.wot');
    }
    var raw = atob(payload.bodyBase64);
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) {
      bytes[i] = raw.charCodeAt(i);
    }
    return bytes;
  }

  function binaryToBlob(payload) {
    return new Blob([binaryToBytes(payload)], {
      type: payload.contentType || 'application/octet-stream',
    });
  }

  function binaryToObjectUrl(payload) {
    const url = URL.createObjectURL(binaryToBlob(payload));
    objectUrls.add(url);
    return url;
  }

  function binaryFromBase64(bodyBase64, contentType) {
    if (typeof bodyBase64 !== 'string') {
      throw new Error('bodyBase64 is required');
    }
    return {
      kind: 'binary',
      contentType: contentType || 'application/octet-stream',
      bodyBase64: bodyBase64,
    };
  }

  function binaryFromBytes(bytes, contentType) {
    if (!(bytes instanceof Uint8Array)) {
      bytes = new Uint8Array(bytes);
    }
    var binary = '';
    var chunkSize = 0x8000;
    for (var i = 0; i < bytes.length; i += chunkSize) {
      var chunk = bytes.subarray(i, i + chunkSize);
      binary += String.fromCharCode.apply(null, Array.from(chunk));
    }
    return {
      kind: 'binary',
      contentType: contentType || 'application/octet-stream',
      bodyBase64: btoa(binary),
      sizeBytes: bytes.length,
    };
  }


  window.wot = Object.freeze({
    readProperty: (id, name, opts) => send('readProperty', id, name, undefined, opts),
    writeProperty: (id, name, value, opts) => send('writeProperty', id, name, value, opts),
    invokeAction: (id, name, input, opts) => send('invokeAction', id, name, input, opts),
    observeProperty: (id, name, cb, opts) => subscribe('observeProperty', id, name, cb, opts),
    subscribeEvent: (id, name, cb, opts) => subscribe('subscribeEvent', id, name, cb, opts),
    unsubscribe, isBinaryPayload, binaryFromBase64, binaryFromBytes,
    binaryToBytes, binaryToBlob, binaryToObjectUrl,
    hostPermissions: () => app?.getHostContext()?.permissions || {},
  });
  // Browser APIs reject with NotAllowedError when a host declines a requested
  // permission. Make that observable even when generated code forgets a catch.
  window.addEventListener('unhandledrejection', event => {
    if (event.reason?.name === 'NotAllowedError') {
      window.dispatchEvent(new CustomEvent('wot-error', {detail: {message: 'The host declined this device permission.'}}));
    }
  });
  window.addEventListener('pagehide', () => { void cleanup(); });
})();
