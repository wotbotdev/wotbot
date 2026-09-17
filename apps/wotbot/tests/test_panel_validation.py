import asyncio
import re
from pathlib import Path

import httpx
import pytest

from wotbot.panels import validate as validate_module
from wotbot.panels.validate import (
    extract_dependencies,
    extract_scripts,
    find_syntax_error,
    validate_external_dependencies,
    validate_panel,
)

LIGHT = "urn:dev:light"


def capability(ops, affordances=(), thing_id=LIGHT):
    return {"thingId": thing_id, "affordances": list(affordances), "ops": list(ops)}


def panel(script):
    return f"<div id='root'></div>\n<script>{script}</script>"


# --- syntax ----------------------------------------------------------------
# The two halves matter equally: catching the breakage, and staying silent on
# valid code. A false rejection sends the agent into a retry loop.


VALID = [
    pytest.param("const x = 1;", id="trivial"),
    pytest.param("const re = /[)]/; const y = 2 / 1;", id="regex-holding-a-bracket"),
    pytest.param("s.replace(/\\//g, '-').split(/,\\s*/);", id="escaped-slash-in-regex"),
    pytest.param("const a = b / c / d;", id="repeated-division"),
    pytest.param("const s = `a ${ `b ${c}` } d`;", id="nested-template"),
    pytest.param("const s = 'it\\'s (fine';", id="bracket-inside-string"),
    pytest.param("// ) unbalanced in a comment\nconst x = 1;", id="line-comment"),
    pytest.param("/* ( */ const x = 1;", id="block-comment"),
    pytest.param("const s = 'a \\\n b';", id="string-line-continuation"),
    pytest.param(
        "const o = { a: 1 }; const f = async () => await g?.(1) ?? 2;", id="modern-syntax"
    ),
    pytest.param("label: for (const x of xs) { if (x) continue label; }", id="labels"),
    pytest.param('const html = `<div class="x">${v}</div>`;', id="markup-in-template"),
]


@pytest.mark.parametrize("source", VALID)
def test_valid_scripts_are_left_alone(source):
    assert find_syntax_error(source) is None


BROKEN = [
    pytest.param("wot.readProperty('a', 'b';", "missing ')'", id="missing-paren"),
    pytest.param("foo(bar, baz\nqux();\n", "line 1", id="truncated-argument-list"),
    pytest.param("const s = `unclosed ${x}", "line 1", id="unclosed-template"),
    pytest.param("const s = 'unclosed;\nconst y = 1;", "line 1", id="unclosed-string"),
    pytest.param("function f() { return 1;", "missing '}'", id="unclosed-block"),
    pytest.param("const x = (1 + 2]);", "line 1", id="mismatched-closer"),
    pytest.param("/* never ends\nconst x = 1;", "line 1", id="unclosed-comment"),
    pytest.param("const x = 1);", "line 1", id="stray-closer"),
    pytest.param(
        # The shape that started this: an inner forEach( never closed.
        "Object.keys(a).forEach(r=>Object.keys(b).forEach(m=>t.push(f(r,m)));\n",
        "missing ')'",
        id="nested-call-missing-paren",
    ),
]


@pytest.mark.parametrize("source,expected", BROKEN)
def test_broken_scripts_are_reported(source, expected):
    error = find_syntax_error(source)
    assert error is not None
    assert expected in error


def test_line_numbers_are_relative_to_the_submitted_markup():
    # The agent gets back a line it can find in the html it just wrote, not one
    # relative to whichever <script> the problem happens to be in.
    html = "<div>a</div>\n<script>const ok = 1;</script>\n<script>\nfoo(bar,\n</script>"
    problems = validate_panel(html, [])
    assert len(problems) == 1
    assert "line 4" in problems[0]


def test_find_syntax_error_offsets_reported_lines():
    error = find_syntax_error("const a = 1;\nfoo(bar,\n", line_offset=10)
    assert "line 12" in error


# --- script extraction -----------------------------------------------------


def test_only_executable_script_types_are_parsed():
    html = (
        '<script type="application/json">{ "unbalanced": ( }</script>'
        '<script type="module">const x = 1;</script>'
        "<script>const y = 2;</script>"
        "<script type=text/javascript>const z = 3;</script>"
    )
    assert [script.source for script in extract_scripts(html)] == [
        "const x = 1;",
        "const y = 2;",
        "const z = 3;",
    ]


def test_scripts_carry_the_line_they_start_on():
    html = "<div>a</div>\n<p>b</p>\n<script>const x = 1;</script>"
    assert [script.first_line for script in extract_scripts(html)] == [3]


def test_markup_outside_scripts_is_not_parsed_as_code():
    assert validate_panel("<p>a ( b { c</p>", []) == []


# --- unverifiable subresource integrity -----------------------------------


@pytest.mark.parametrize(
    "html",
    [
        '<link rel="stylesheet" href="https://unpkg.com/x.css" integrity="sha256-wrong">',
        "<script src='https://unpkg.com/x.js' INTEGRITY='sha384-wrong'></script>",
        '<img src="data:image/png;base64,eA==" integrity>',
    ],
)
def test_integrity_attributes_are_rejected(html):
    problems = validate_panel(html, [])
    assert len(problems) == 1
    assert "Remove every integrity attribute" in problems[0]


def test_integrity_text_is_not_mistaken_for_an_attribute():
    html = '<p>Remove integrity="sha256-example" from the tag.</p>'
    assert validate_panel(html, []) == []


# --- external dependencies -------------------------------------------------


def test_extracts_scripts_modules_and_stylesheets():
    html = """
      <link rel="stylesheet" href="https://cdnjs.cloudflare.com/a.css">
      <script src="https://cdn.plot.ly/a.js"></script>
      <script type="module">
        import x from "https://cdn.jsdelivr.net/a.js";
        import "https://unpkg.com/b.js";
        import mapped from "x";
      </script>
      <script type="importmap">
        {"imports":{"x":"https://cdn.jsdelivr.net/x.js"},
         "scopes":{"/":{"y":"https://unpkg.com/y.js"}}}
      </script>
    """
    assert [(item.url, item.kind) for item in extract_dependencies(html)] == [
        ("https://cdnjs.cloudflare.com/a.css", "stylesheet"),
        ("https://cdn.plot.ly/a.js", "script"),
        ("https://cdn.jsdelivr.net/a.js", "module"),
        ("https://unpkg.com/b.js", "module"),
    ]


def test_unavailable_dependency_is_rejected_for_any_library():
    def respond(request):
        assert request.headers["range"] == "bytes=0-0"
        return httpx.Response(404)

    problems = asyncio.run(
        validate_external_dependencies(
            '<script src="https://cdn.jsdelivr.net/npm/example/missing.js"></script>',
            transport=httpx.MockTransport(respond),
        )
    )
    assert len(problems) == 1
    assert "HTTP 404" in problems[0]
    assert "example/missing.js" in problems[0]


def test_available_dependency_passes():
    problems = asyncio.run(
        validate_external_dependencies(
            '<script type="module" src="https://unpkg.com/example.js"></script>',
            transport=httpx.MockTransport(lambda _request: httpx.Response(206)),
        )
    )
    assert problems == []


def test_disallowed_dependency_host_is_rejected_without_requesting_it():
    def must_not_run(_request):
        raise AssertionError("disallowed hosts must never be contacted")

    problems = asyncio.run(
        validate_external_dependencies(
            '<script src="https://example.com/library.js"></script>',
            transport=httpx.MockTransport(must_not_run),
        )
    )
    assert len(problems) == 1
    assert "cannot load under the panel CSP" in problems[0]


def test_redirect_outside_allowlist_is_rejected():
    def respond(request):
        return httpx.Response(302, headers={"Location": "https://example.com/library.js"})

    problems = asyncio.run(
        validate_external_dependencies(
            '<script src="https://unpkg.com/library.js"></script>',
            transport=httpx.MockTransport(respond),
        )
    )
    assert len(problems) == 1
    assert "cannot load under the panel CSP" in problems[0]


def test_unmapped_bare_import_in_inline_module_is_rejected_without_request():
    def must_not_run(_request):
        raise AssertionError("an unresolved bare import must fail before fetching")

    problems = asyncio.run(
        validate_external_dependencies(
            '<script type="module">import x from "package-name";</script>',
            transport=httpx.MockTransport(must_not_run),
        )
    )
    assert len(problems) == 1
    assert "bare module specifier 'package-name'" in problems[0]
    assert "no matching import-map entry" in problems[0]


def test_unmapped_bare_import_inside_downloaded_module_is_rejected():
    def respond(request):
        assert request.url.path == "/addon.js"
        return httpx.Response(200, text='import { x } from "package-name";')

    problems = asyncio.run(
        validate_external_dependencies(
            '<script type="module" src="https://unpkg.com/addon.js"></script>',
            transport=httpx.MockTransport(respond),
        )
    )
    assert len(problems) == 1
    assert "bare module specifier 'package-name'" in problems[0]
    assert "https://unpkg.com/addon.js" in problems[0]


def test_import_map_resolves_bare_imports_across_downloaded_modules():
    requested = []

    def respond(request):
        requested.append(str(request.url))
        if request.url.path == "/addon.js":
            return httpx.Response(200, text='import { x } from "package-name";')
        if request.url.path == "/package.js":
            return httpx.Response(200, text="export const x = 1;")
        raise AssertionError(f"unexpected dependency: {request.url}")

    html = """
      <script type="importmap">
        {"imports":{"package-name":"https://unpkg.com/package.js"}}
      </script>
      <script type="module" src="https://unpkg.com/addon.js"></script>
    """
    problems = asyncio.run(
        validate_external_dependencies(html, transport=httpx.MockTransport(respond))
    )
    assert problems == []
    assert requested == ["https://unpkg.com/addon.js", "https://unpkg.com/package.js"]


def test_dependency_hosts_match_panel_csp():
    csp = Path(__file__).resolve().parents[2] / "ui" / "src" / "lib" / "panel-csp.ts"
    if not csp.exists():
        pytest.skip("panel CSP source is not part of this checkout")
    source = csp.read_text()

    def hosts(constant):
        body = re.search(rf"const {constant} = \[(.*?)\];", source, re.DOTALL).group(1)
        return set(re.findall(r"'https://([^']+)'", body))

    assert hosts("SCRIPT_CDNS") == validate_module._SCRIPT_HOSTS
    assert hosts("SCRIPT_CDNS") | hosts("CDN_HOSTS") == validate_module._DEPENDENCY_HOSTS


def test_script_from_font_host_is_rejected_but_stylesheet_is_not():
    html = """
      <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">
      <script src="https://fonts.googleapis.com/library.js"></script>
    """
    problems = asyncio.run(
        validate_external_dependencies(
            html, transport=httpx.MockTransport(lambda _request: httpx.Response(200))
        )
    )
    assert len(problems) == 1
    assert "library.js" in problems[0]
    assert "cannot load under the panel CSP" in problems[0]


def test_export_from_is_followed():
    def respond(request):
        if request.url.path == "/index.js":
            return httpx.Response(200, text='export * from "package-name";')
        raise AssertionError(f"unexpected dependency: {request.url}")

    problems = asyncio.run(
        validate_external_dependencies(
            '<script type="module" src="https://unpkg.com/index.js"></script>',
            transport=httpx.MockTransport(respond),
        )
    )
    assert len(problems) == 1
    assert "bare module specifier 'package-name'" in problems[0]


def test_large_module_graph_stops_quietly_at_the_limit():
    requested = []

    def respond(request):
        requested.append(request.url.path)
        index = int(request.url.path.strip("/m.js") or 0)
        return httpx.Response(200, text=f'import "https://unpkg.com/m{index + 1}.js";')

    problems = asyncio.run(
        validate_external_dependencies(
            '<script type="module" src="https://unpkg.com/m0.js"></script>',
            transport=httpx.MockTransport(respond),
        )
    )
    assert problems == []
    assert len(requested) == validate_module._MAX_DEPENDENCIES


def test_redirect_loop_stays_silent():
    problems = asyncio.run(
        validate_external_dependencies(
            '<script src="https://unpkg.com/a.js"></script>',
            transport=httpx.MockTransport(
                lambda request: httpx.Response(302, headers={"Location": str(request.url)})
            ),
        )
    )
    assert problems == []


def test_truncated_module_is_not_crawled(monkeypatch):
    monkeypatch.setattr(validate_module, "_MAX_MODULE_BYTES", 16)

    def respond(request):
        assert request.headers["range"] == "bytes=0-16"
        if request.url.path == "/big.js":
            # A server honouring Range returns limit + 1 bytes of a larger file.
            return httpx.Response(206, text='import "missing";')
        raise AssertionError(f"unexpected dependency: {request.url}")

    problems = asyncio.run(
        validate_external_dependencies(
            '<script type="module" src="https://unpkg.com/big.js"></script>',
            transport=httpx.MockTransport(respond),
        )
    )
    assert problems == []


def test_deadline_keeps_problems_already_found(monkeypatch):
    monkeypatch.setattr(validate_module, "_DEPENDENCY_DEADLINE_SECONDS", 0.2)

    async def respond(request):
        if request.url.path == "/missing.js":
            return httpx.Response(404)
        await asyncio.sleep(5)
        return httpx.Response(200)

    html = """
      <script src="https://unpkg.com/missing.js"></script>
      <script type="module" src="https://unpkg.com/slow.js"></script>
    """
    problems = asyncio.run(
        validate_external_dependencies(html, transport=httpx.MockTransport(respond))
    )
    assert len(problems) == 1
    assert "HTTP 404" in problems[0]


# --- bridge calls ----------------------------------------------------------


def test_undeclared_bridge_call_is_reported():
    html = panel("wot.readProperty('urn:dev:other', 'state');")
    problems = validate_panel(html, [capability(["readProperty"], ["state"])])
    assert len(problems) == 1
    assert "urn:dev:other" in problems[0]


def test_call_using_an_op_that_was_not_declared_is_reported():
    html = panel("wot.writeProperty('urn:dev:light', 'state', 'red');")
    problems = validate_panel(html, [capability(["readProperty"], ["state"])])
    assert "writeProperty" in problems[0]


def test_empty_affordance_list_permits_any_affordance():
    # Mirrors isInteractionAllowed: an empty list means "any on this thing".
    html = panel("wot.readProperty('urn:dev:light', 'anything');")
    assert validate_panel(html, [capability(["readProperty"])]) == []


def test_window_prefixed_and_declared_calls_pass():
    html = panel(
        "await window.wot.readProperty('urn:dev:light', 'state');\n"
        "wot.observeProperty('urn:dev:light', 'state', (v) => render(v));"
    )
    caps = [capability(["readProperty", "observeProperty"], ["state"])]
    assert validate_panel(html, caps) == []


def test_calls_with_computed_arguments_are_not_guessed_at():
    # Only literal/literal calls are checked; anything else could be legitimate.
    html = panel("wot.readProperty(thingId, nameFor(x));")
    assert validate_panel(html, [capability(["readProperty"])]) == []


def test_each_distinct_call_is_reported_once():
    html = panel(
        "wot.readProperty('urn:dev:x', 'a');\n"
        "wot.readProperty('urn:dev:x', 'a');\n"
        "wot.readProperty('urn:dev:x', 'b');"
    )
    assert len(validate_panel(html, [])) == 2


# --- capabilities against the real Thing Description -----------------------

AFFORDANCES = {LIGHT: {"properties": ["state", "brightness"], "actions": ["toggle"], "events": []}}


def test_affordance_missing_from_the_thing_is_reported():
    problems = validate_panel(
        panel("const x = 1;"),
        [capability(["readProperty"], ["dimLevel"])],
        AFFORDANCES,
    )
    assert len(problems) == 1
    assert "no 'dimLevel'" in problems[0]
    assert "brightness" in problems[0]


def test_op_that_does_not_match_the_affordance_kind_is_reported():
    problems = validate_panel(
        panel("const x = 1;"),
        [capability(["readProperty"], ["toggle"])],
        AFFORDANCES,
    )
    assert "is an action" in problems[0]


def test_declared_affordances_that_exist_pass():
    caps = [
        capability(["readProperty", "writeProperty"], ["state", "brightness"]),
        capability(["invokeAction"], ["toggle"]),
    ]
    assert validate_panel(panel("const x = 1;"), caps, AFFORDANCES) == []


def test_things_absent_from_the_lookup_are_not_complained_about():
    # A registry lookup that failed must never read as a broken panel.
    caps = [capability(["readProperty"], ["whatever"], thing_id="urn:dev:unknown")]
    assert validate_panel(panel("const x = 1;"), caps, AFFORDANCES) == []


# --- blocked egress --------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "fetch('/api/things');",
        "const r = new XMLHttpRequest();",
        "const s = new WebSocket('wss://example.com');",
        "navigator.sendBeacon('/x', d);",
    ],
)
def test_csp_blocked_egress_is_reported(source):
    problems = validate_panel(panel(source), [])
    assert len(problems) == 1
    assert "window.wot" in problems[0]


def test_lookalike_identifiers_are_not_flagged():
    html = panel("obj.fetch(1); myFetch(2); const prefetch = 3;")
    assert validate_panel(html, []) == []


# --- what parsing buys over pattern matching -------------------------------


def test_blocked_apis_named_in_strings_and_comments_are_not_calls():
    html = panel(
        "// never use fetch( here\nconst note = 'call fetch() and it is blocked';\nrender(note);"
    )
    assert validate_panel(html, []) == []


def test_method_named_like_a_blocked_global_is_not_flagged():
    html = panel("cache.fetch(1); const s = new store.WebSocket();")
    assert validate_panel(html, []) == []


def test_window_prefixed_fetch_is_still_flagged():
    problems = validate_panel(panel("window.fetch('/x');"), [])
    assert len(problems) == 1
    assert "fetch()" in problems[0]


def test_bridge_calls_split_across_lines_are_found():
    html = panel("await wot.readProperty(\n  'urn:dev:other',  // the wrong thing\n  'state',\n);")
    problems = validate_panel(html, [capability(["readProperty"], ["state"])])
    assert len(problems) == 1
    assert "urn:dev:other" in problems[0]


def test_bridge_calls_named_in_a_string_are_not_calls():
    html = panel("const help = \"call wot.readProperty('urn:dev:other', 'state')\";")
    assert validate_panel(html, []) == []


def test_optional_call_on_the_bridge_is_still_a_call():
    html = panel("wot?.readProperty('urn:dev:other', 'state');")
    assert len(validate_panel(html, [])) == 1


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import * as THREE from 'https://cdn/three.js';", id="module-import"),
        pytest.param("class A { #x = 1; static y = 2; }", id="private-class-field"),
        pytest.param("const d = await load();", id="top-level-await"),
        pytest.param("const v = a?.b ?? c;", id="optional-and-nullish"),
        pytest.param("for await (const c of stream) { use(c); }", id="for-await"),
    ],
)
def test_modern_syntax_parses(source):
    assert find_syntax_error(source) is None
