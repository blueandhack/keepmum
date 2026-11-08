"""Codex code mode: each literal of an `exec` script gets the real values its nested tool may receive."""

import json
from collections import Counter

import pytest

from keepmum.codemode import parse, script_strings
from keepmum.restore import DEFAULT_POLICY, TOOL, restore_tool
from keepmum.vault import Vault

TOKEN = "ghp_" + "a1B2c3D4" * 4 + "Zz9q"
EMAIL = "margaret@thornbury.example"
PERSON = "Margaret Thornbury"


@pytest.fixture
def v():
    vault = Vault(bytes(range(32)))
    vault.ph = {"tok": vault.placeholder("SECRET", TOKEN, ("rules:github-token",)),
                "mail": vault.placeholder("EMAIL", EMAIL),
                "person": vault.placeholder("PERSON", PERSON),
                "own": vault.placeholder("EMAIL", "ops@notes.example", origin="mcp__notes")}
    return vault


def run(script: str, vault, policy=DEFAULT_POLICY, counts=None) -> str:
    return restore_tool("exec", script, vault, policy, Counter() if counts is None else counts)


def test_a_literal_passed_to_an_mcp_tool_keeps_its_placeholder(v):
    ph = v.ph
    out = run(f'await tools.mcp__notes__send_note({{text: "deploy token {ph["tok"]}"}});', v)
    assert ph["tok"] in out and TOKEN not in out
    out = run(f'await tools.mcp__notes__send_note({{text: "write to {ph["mail"]}"}});', v)
    assert ph["mail"] in out and EMAIL not in out
    # Its own value goes back to the server that returned it, as for a direct MCP call.
    assert "ops@notes.example" in run(f'await tools.mcp__notes__send_note({{text: "{ph["own"]}"}});', v)


def test_a_command_in_the_same_script_still_gets_the_real_value(v):
    ph = v.ph
    script = (f'await tools.exec_command({{cmd: "printf %s {ph["tok"]} > .token"}});\n'
              f'await tools.mcp__notes__send_note({{text: "rotated {ph["tok"]}"}});')
    out = run(script, v)
    assert f"printf %s {TOKEN} > .token" in out
    assert f'send_note({{text: "rotated {ph["tok"]}"}})' in out


@pytest.mark.parametrize("script", [
    'const t = "{tok}";\nawait tools.mcp__notes__send_note({{text: t}});',
    'const note = `deploy ${{"{tok}"}}`;\nawait tools.mcp__notes__send_note({{text: note}});',
    'const send = (t) => tools.mcp__notes__send_note({{text: t}});\nawait send("{tok}");',
    'const x = `${{await tools.mcp__notes__send_note({{text: "{tok}"}})}}`;',
    'const s = tools.mcp__notes__send_note;\nawait s({{text: "{tok}"}});',
    'const q = "a\\"b".replace(/"/g, "");\nawait tools.mcp__notes__send_note({{text: "{tok}"}});',
    # a function of the script's own between the literal and the command: it can go anywhere
    'const send = (t) => tools.mcp__notes__send_note({{text: t}});\n'
    'await tools.exec_command({{cmd: send("{tok}")}});',
])
def test_a_literal_that_can_reach_an_mcp_tool_keeps_its_placeholder(v, script):
    script = script.format(tok=v.ph["tok"])
    out = run(script, v)
    assert TOKEN not in out and v.ph["tok"] in out


@pytest.mark.parametrize("script", [
    'const f = tools[ALL_TOOLS.find(t => t.name.includes("send")).name];\nawait f({{text: "{x}"}});',
    'for (const t of ALL_TOOLS) await tools[t.name]({{text: "{x}"}});',
    'eval("tools.mcp__notes__send_note({{text: \'{x}\'}})");',
    'const {{mcp__notes__send_note: s}} = tools;\nawait s({{text: "{x}"}});',
    'await globalThis["tools"]["mcp__notes__send_note"]({{text: "{x}"}});',
])
def test_a_script_that_reaches_tools_without_naming_them_gets_no_free_values(v, script):
    """A literal it may pass anywhere gets nothing, a PERSON too, which a command would get."""
    counts = Counter()
    out = run(script.format(x=v.ph["person"]), v, counts=counts)
    assert PERSON not in out
    assert counts[(TOOL, "exec", "keep", "script", "PERSON")] == 1


def test_a_dynamic_script_still_runs_its_commands_with_real_values(v):
    script = (f'await tools.exec_command({{cmd: "echo {v.ph["tok"]} > .token"}});\n'
              'for (const t of ALL_TOOLS) text(t.name);\nconst f = tools[name];')
    assert f"echo {TOKEN} > .token" in run(script, v)


def test_what_a_server_may_receive_goes_in_as_it_is_not_quoted_for_a_shell(v):
    policy = {**DEFAULT_POLICY, "mcp__notes__*": ["PERSON", "EMAIL"]}
    counts = Counter()
    out = run(f'await tools.mcp__notes__send_note({{text: "for {v.ph["person"]}"}});', v, policy, counts)
    assert f'"for {PERSON}"' in out
    assert counts[(TOOL, "mcp__notes__send_note", "restore", "allowed", "PERSON")] == 1
    # Free literals: what every network tool the script names may receive.
    out = run(f'const who = "{v.ph["person"]}";\nawait tools.mcp__notes__send_note({{text: who}});', v, policy)
    assert f'"{PERSON}"' in out
    out = run(f'const who = "{v.ph["person"]}";\nawait tools.mcp__notes__send_note({{text: who}});\n'
              f'await tools.mcp__other__post({{text: who}});', v, policy)
    assert PERSON not in out


def test_a_nested_tool_that_is_not_a_command_gets_its_value_unquoted(v):
    out = run(f'await tools.view_image({{path: "/tmp/{v.ph["person"]}.png"}});', v)
    assert f'"/tmp/{PERSON}.png"' in out
    out = run(f'await tools.exec_command({{cmd: "open /tmp/{v.ph["person"]}.png"}});', v)
    assert f"\"open /tmp/'{PERSON}'.png\"" in out


def test_an_mcp_resource_is_its_servers(v):
    policy = {**DEFAULT_POLICY, "mcp__notes__*": ["EMAIL"]}
    out = run(f'await tools.read_mcp_resource({{server: "notes", uri: "notes://{v.ph["mail"]}"}});', v, policy)
    assert EMAIL in out
    out = run(f'await tools.read_mcp_resource({{server: "other", uri: "other://{v.ph["mail"]}"}});', v, policy)
    assert EMAIL not in out


def test_decisions_name_the_tool_that_decided(v):
    counts = Counter()
    run(f'await tools.exec_command({{cmd: "echo {v.ph["mail"]}"}});\n'
        f'await tools.mcp__notes__send_note({{text: "{v.ph["mail"]}"}});', v, counts=counts)
    assert counts[(TOOL, "exec", "restore", "local", "EMAIL")] == 1
    assert counts[(TOOL, "mcp__notes__send_note", "keep", "not_allowed", "EMAIL")] == 1


def test_parse_reads_calls_templates_and_regular_expressions():
    src = ('const a = "x";\nawait tools.exec_command({cmd: `ls ${a} "${b}"`});\n'
           'const r = /["`]/g;\nawait tools.mcp__n__t({q: "y"});\napply_patch("p");')
    script = parse(src)
    assert [(src[x.start:x.end], x.call) for x in script.literals] == [
        ("x", None), ("ls ", "exec_command"), (' "', "exec_command"), ('"', "exec_command"),
        ("y", "mcp__n__t"), ("p", "apply_patch")]
    assert script.calls == {"exec_command", "mcp__n__t", "apply_patch"} and not script.dynamic
    assert script_strings(src) == ["x", 'ls ${a} "${b}"']
    assert parse("text(ALL_TOOLS.length); x.tools = 1;").dynamic is False
    assert parse("Object.keys(tools)").dynamic is True


def test_a_template_command_is_quoted_as_one_command(v):
    out = run(f'await tools.exec_command({{cmd: `cd ${{dir}} && echo "{v.ph["person"]}"`}});', v)
    assert f'echo "{PERSON}"' in out


def test_the_streamed_script_is_decided_the_same(v):
    from keepmum.responses import ResponsesStreamRestorer

    script = f'await tools.mcp__notes__send_note({{text: "{v.ph["tok"]}"}});'

    def ev(d):
        return f"event: {d['type']}\ndata: {json.dumps(d)}\n\n".encode()
    r = ResponsesStreamRestorer(v)
    out = b"".join(r.feed(e) for e in [
        ev({"type": "response.output_item.added", "output_index": 0,
            "item": {"type": "custom_tool_call", "id": "ct", "name": "exec", "input": ""}}),
        ev({"type": "response.custom_tool_call_input.delta", "item_id": "ct", "output_index": 0, "delta": script[:30]}),
        ev({"type": "response.custom_tool_call_input.delta", "item_id": "ct", "output_index": 0, "delta": script[30:]}),
        ev({"type": "response.custom_tool_call_input.done", "item_id": "ct", "output_index": 0, "input": script})]).decode()
    assert v.ph["tok"] in out and TOKEN not in out


def _calls(script: str) -> list[list]:
    """Run a script in node against stub tools: [tool, input] for each call it makes."""
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    stub = ("const calls = []; const text = () => {}; const tools = new Proxy({}, {get: (_, name) => "
            "async (input) => { calls.push([name, input]); return {output: ''}; }});\n"
            "(async () => {\n" + script + "\n})().then(() => console.log(JSON.stringify(calls)));")
    done = subprocess.run(["node", "-e", stub], capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.parametrize("script, cmd", [
    ('const tok = "{tok}";\nawait tools.exec_command({{cmd: "printf %s " + tok + " > .token"}});',
     "printf %s {TOKEN} > .token"),
    ('const tok = "{tok}";\nawait tools.exec_command({{cmd: `printf %s ${{tok}} > .token`}});',
     "printf %s {TOKEN} > .token"),
    ('const cmd = "echo {tok} > .token";\nawait tools.exec_command({{cmd}});', "echo {TOKEN} > .token"),
    ('const who = "{person}";\nawait tools.exec_command({{cmd: "touch " + who}});', "touch 'Margaret Thornbury'"),
])
def test_a_name_shared_by_a_command_and_a_connector_gets_the_value_only_in_the_command(v, script, cmd):
    """`const tok = "…"` used by both: the command gets the real value written in its place."""
    script = script.format(tok=v.ph["tok"], person=v.ph["person"])
    script += '\nawait tools.mcp__notes__send_note({text: "done: " + ' + script.split()[1] + '});'
    counts = Counter()
    calls = _calls(run(script, v, counts=counts))
    assert calls[0] == ["exec_command", {"cmd": cmd.format(TOKEN=TOKEN)}]
    sent = json.dumps(calls[1])
    assert calls[1][0] == "mcp__notes__send_note" and TOKEN not in sent and PERSON not in sent
    assert counts[(TOOL, "exec", "restore", "local", "SECRET")] + counts[(TOOL, "exec", "restore", "local", "PERSON")] == 1


@pytest.mark.parametrize("script", [
    'let tok = "{tok}";\ntok = tok.trim();\nawait tools.exec_command({{cmd: "echo " + tok}});',
    'const tok = "{tok}";\nconst f = (tok) => tools.exec_command({{cmd: "echo " + tok}});\nawait f("x");',
    'const tok = "{tok}";\nfunction f(a, tok) {{ return tools.exec_command({{cmd: "echo " + tok}}); }}\nawait f(1, "x");',
    'const tok = "{tok}";\nfor (const tok of ["x"]) await tools.exec_command({{cmd: "echo " + tok}});',
    'const tok = "{tok}" + "";\nawait tools.exec_command({{cmd: "echo " + tok}});',
])
def test_a_name_that_may_hold_something_else_is_not_written_in(v, script):
    """Reassigned, shadowed, or not just a literal: keepmum cannot tell what it holds there."""
    script = script.format(tok=v.ph["tok"]) + '\nawait tools.mcp__notes__send_note({text: tok});'
    assert TOKEN not in run(script, v)


@pytest.mark.parametrize("script, own", [
    ('text(await tools.mcp__notes__lookup({name: "ops"}));', True),
    ('const a = await tools.mcp__notes__lookup({name: "ops"});\ntext(await tools.mcp__notes__send_note({text: "x"}));', True),
    ('text(await tools.mcp__notes__lookup({name: "ops"}));\ntext(await tools.exec_command({cmd: "cat .env"}));', False),
    ('text(await tools.mcp__notes__lookup({name: "ops"}));\ntext(await tools.mcp__other__find({q: "x"}));', False),
    ('text(await tools[n]({name: "ops"}));', False),
])
def test_what_a_script_returns_from_one_server_can_go_back_to_it(script, own):
    """A script that calls one MCP server's tools and nothing else returns that server's results."""
    from keepmum.detect.pipeline import Pipeline
    from keepmum.detect.rules import RulesDetector
    from keepmum.redact import Redactor

    vault = Vault(bytes(range(32)))
    body = {"input": [
        {"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": script},
        {"type": "custom_tool_call_output", "call_id": "c1", "output": "found: margaret.thornbury@gmail.com"}]}
    out = Redactor(Pipeline(RulesDetector(), pii=True), vault).redact_responses_request(body)
    ph = next(iter(vault._values))
    assert ph in json.dumps(out) and vault.origins_of("margaret.thornbury@gmail.com") == ({"mcp__notes"} if own else {"exec"})
    sent = run(f'await tools.mcp__notes__send_note({{text: "reply to {ph}"}});', vault)
    assert ("margaret.thornbury@gmail.com" in sent) == own
