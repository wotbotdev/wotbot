# Generated panel browser acceptance

This fixture renders `panel-fixture.html` through the real WoTBot panel wrapper
and its `window.wot` bridge. Device responses are fixtures; backend integration
tests separately exercise the runtime proxy and Redis subscriptions.

## Automated run

`test_acceptance.py` drives both panel checks headlessly and asserts the same
verdicts described below. Chromium is much larger than the other test
dependencies, so it installs separately:

```bash
cd apps/wotbot
uv pip install -e '.[browser]'
.venv/bin/playwright install chromium
.venv/bin/python -m pytest tests/browser -m browser
```

The data-panel checks load Leaflet and OpenStreetMap tiles over the network and
are marked `external` as well; deselect them with `-m 'browser and not external'`.
The UI deep-link check at the end of this file is not automated and stays manual.

`test_validation.py` exercises the production browser gate on generated documents:
exact attachments, runtime exceptions, rejected promises, failed/empty self-checks,
blank output, missing readiness, unavailable dependencies, forbidden redirects,
blocked live calls and a hung renderer followed by a successful request. The
external Leaflet test also runs the real data fixture through this gate. The
ordinary unit tests in `test_browser_validation.py` and `test_create_web_interface.py`
check CSP parity, service failure handling and that no failed panel is published.

`test_live_validation.py` exercises the complete backend/WebSocket/worker/Chromium
round trip with a mocked runtime client: actual decoded values, mixed attachments,
binary payloads, concurrent reads, wrong data shapes, unavailable devices,
undeclared reads and blocked actions. It also verifies that runtime credentials
never enter the validation request and that action controls are not clicked.

The worker checks initialization, captures 1000×650 and 390×650 screenshots and
rejects uniform blank output. The caller performs a separate advisory image
review. Browser tests do not call a model; `test_panel_visual_review.py` tests its
contract and outage handling. The [evaluation harness](../../scripts/evaluate_panels.py)
uses real model calls and records evidence for independent visual assessment.

## Model evaluation

With a running stack, current migrations, the browser validator and model
credentials with vision support, run from the repository root:

```bash
docker compose exec -T wotbot python -u - \
  --output /tmp/panel-evaluation --fixture-host wotbot \
  < apps/wotbot/scripts/evaluate_panels.py
docker compose cp wotbot:/tmp/panel-evaluation ./panel-evaluation
```

This incurs model usage for ten map, chart and live-property cases, including
repairs and visual reviews. Add `--runs 1` for a smoke check. The output directory
must be new, and the fixture host must be reachable from the WoT runtime.
The harness exports generated HTML, reports, screenshots and usage metrics before
removing its temporary Thing, attachment and chat metadata. HTML artifacts retain
their normal executor expiry.

Inspect both screenshot widths independently when assessing visual warnings and
missed defects. The harness covers panel generation and initial live-property
reads; it does not evaluate discovery, preceding analysis or continuous updates.

## Manual run

Useful when inspecting a panel by eye, which is the part the automated run does
not cover: tile imagery, layout and anything else that is only visible.

From the repository root:

```bash
.venv/bin/python -m uvicorn \
  --app-dir apps/wotbot/tests/browser serve:app --host 0.0.0.0 --port 8918
```

Open `http://localhost:8918/`. The panel must show **ALL PANEL CHECKS PASSED**.
Checks cover binary reads/writes/actions, property observation, event
subscriptions, and unsubscribe.

Open `http://localhost:8918/data-panel` for the data attachment acceptance check.
It renders a polygon with a hole over OpenStreetMap using an attachment larger
than the executor stdout limit. The page must show **ALL ATTACHMENT CHECKS PASSED**,
with map tiles visible. It compares Leaflet's full precision output with the
attached coordinates, checks fresh reads after mutation, rejects unknown names,
and checks that a `</script>` string in JSON cannot execute. Run this in a
sandboxed iframe too when checking the UI. Leaflet and map tiles require network
access; geometry/attachment failures are shown separately in the page status.

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
