import pytest

from wotbot.panels.validate import (
    extract_scripts,
    find_syntax_error,
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
