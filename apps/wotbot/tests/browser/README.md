# Generated panel browser acceptance

This fixture renders the same `panel-fixture.html` through the real WoTBot and
MCP wrappers. The MCP host uses the official `@modelcontextprotocol/ext-apps`
AppBridge, a separate proxy origin, and an opaque sandbox. Device responses and
the launch grant are fixtures. Backend integration tests separately exercise
the real MCP HTTP server, grant validation, allowlists, and Redis subscriptions.

From the repository root, install/bundle the official host SDK in a temporary
directory (requires npm/network):

```bash
fixture_dir=$(mktemp -d)
npm install --prefix "$fixture_dir" @modelcontextprotocol/ext-apps@1.7.5 esbuild
cat > "$fixture_dir/host-entry.js" <<'JS'
export { AppBridge, PostMessageTransport } from '@modelcontextprotocol/ext-apps/app-bridge';
JS
"$fixture_dir/node_modules/.bin/esbuild" "$fixture_dir/host-entry.js" \
  --bundle --format=esm --outfile="$fixture_dir/host.js"
MCP_HOST_BUNDLE="$fixture_dir/host.js" .venv/bin/python -m uvicorn \
  --app-dir apps/wotbot/tests/browser serve:app --host 0.0.0.0 --port 8918
```

Open `http://localhost:8918/` and `http://localhost:8918/normal`. Both panels
must show **ALL PANEL CHECKS PASSED**. Checks cover startup before host
initialization/grant delivery, binary reads/writes/actions, property observation,
event subscriptions, and unsubscribe. The MCP host log must include
**Official AppBridge initialized**. Its proxy uses `127.0.0.1:8918` intentionally
to exercise the separate origin; keep the documented port.

For the existing UI deep-link check, start the UI in another terminal:

```bash
cd apps/ui
WOTBOT_URL=http://localhost:8918 WOT_API_URL=http://localhost:8918/api \
  INTERNAL_API_KEY=fixture INIT_ADMIN_TOKEN=fixture npm run dev -- --port 3117
```

Open `http://localhost:3117/panels?panelId=panel-fixture`. The saved panel drawer
must open and show **A2A panel link opened successfully**. Closing removes the
query parameter. Opening `/panels?panelId=missing-fixture` shows a dismissible
unavailable-panel message. Stop both development servers when finished.
