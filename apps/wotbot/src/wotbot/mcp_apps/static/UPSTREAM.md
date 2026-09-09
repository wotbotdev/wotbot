# Vendored MCP Apps SDK

`ext-apps-1.7.5.js` is the unmodified `dist/src/app-with-deps.js` bundle from
[`@modelcontextprotocol/ext-apps`](https://github.com/modelcontextprotocol/ext-apps)
version 1.7.5, under the included licence.

WoTBot serves this pinned bundle from its backend origin, so loading the bridge
does not require a third-party CDN. Generated panels may still use the libraries
and map tiles declared in their MCP Apps CSP metadata.

To update: install the selected `@modelcontextprotocol/ext-apps` version in a
temporary directory, copy its `app-with-deps.js` and licence here, and change
`EXT_APPS_VERSION` in `wotbot/mcp_apps/assets.py`. Update the browser acceptance
host dependency in `tests/browser/README.md` and run its checks. The filename is
version-stamped because it is served immutable.
