# Generated panel browser acceptance

This fixture renders `panel-fixture.html` through the real WoTBot panel wrapper
and its `window.wot` bridge. Device responses are fixtures; backend integration
tests separately exercise the runtime proxy and Redis subscriptions.

From the repository root:

```bash
.venv/bin/python -m uvicorn \
  --app-dir apps/wotbot/tests/browser serve:app --host 0.0.0.0 --port 8918
```

Open `http://localhost:8918/`. The panel must show **ALL PANEL CHECKS PASSED**.
Checks cover binary reads/writes/actions, property observation, event
subscriptions, and unsubscribe.

For the UI deep-link check, start the UI in another terminal:

```bash
cd apps/ui
WOTBOT_URL=http://localhost:8918 WOT_API_URL=http://localhost:8918/api \
  INTERNAL_API_KEY=fixture INIT_ADMIN_TOKEN=fixture npm run dev -- --port 3117
```

Open `http://localhost:3117/panels?panelId=panel-fixture`. The saved panel drawer
must open and show **A2A panel link opened successfully**. Closing removes the
query parameter. Opening `/panels?panelId=missing-fixture` shows a dismissible
unavailable-panel message. Stop both development servers when finished.
