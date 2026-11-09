"""A real value goes back in the syntax around its placeholder (encode.py), so the shell, JavaScript
or JSON reads exactly the value the user had: not `Xk9` for `Xk9$mQ2…`, not `c0rrecthorse`."""

import json
import subprocess
from collections import Counter

import pytest

from keepmum.detect.pipeline import Pipeline
from keepmum.detect.rules import RulesDetector
from keepmum.encode import shell_contexts, shell_quote
from keepmum.redact import Redactor
from keepmum.restore import DEFAULT_POLICY, restore_text, restore_tool
from keepmum.vault import Vault

HARD = ["Xk9$mQ2vL7pZw4", "c0rrect\\horse9Zq", 'Tr0ub4dor"&\\3x', "it's `x` $(y) !z", "two\nlines"]


def vault_with(value: str):
    v = Vault(bytes(range(32)))
    return v, v.placeholder("SECRET", value, ["rules:secret-assignment"])


def run(cmd: str) -> str:
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=10).stdout


@pytest.mark.parametrize("value", HARD)
@pytest.mark.parametrize("template", [
    "printf '%s' {ph}",
    "printf '%s' \"{ph}\"",
    "printf '%s' '{ph}'",
    "printf '%s' \"DB_PASSWORD={ph}\"",
    "X={ph}; printf '%s' \"$X\"",
    "cat <<EOF\n{ph}\nEOF",
    "cat <<'EOF'\n{ph}\nEOF",
    "printf '%s' \"$(printf '%s' {ph})\"",
])
def test_a_command_prints_the_real_value(value, template):
    """Run for real: whatever quoting the model wrote around the placeholder, bash gets the value."""
    v, ph = vault_with(value)
    cmd = restore_tool("Bash", {"command": template.format(ph=ph)}, v, DEFAULT_POLICY, Counter())["command"]
    want = ("DB_PASSWORD=" if "DB_PASSWORD" in template else "") + value + ("\n" if template.startswith("cat") else "")
    assert run(cmd) == want, cmd


def test_a_plain_token_is_put_back_as_it_is():
    v, ph = vault_with("ghp_" + "a1B2c3D4" * 4 + "Zz9q")
    cmd = f"git push https://x:{ph}@github.com/o/r.git && echo \"{ph}\""
    out = restore_tool("Bash", {"command": cmd}, v, DEFAULT_POLICY, Counter())["command"]
    assert out == cmd.replace(ph, v.value(ph))


def test_contexts():
    cmd = "a P \"b P $(c P) P\" 'P' <<-E\n\tP\n\tE\nP"
    pos = [i for i, c in enumerate(cmd) if c == "P"]
    assert [shell_contexts(cmd, pos)[p] for p in pos] == ["unquoted", "double", "unquoted", "double", "single",
                                                          "heredoc", "unquoted"]
    assert shell_quote("a b", "unquoted") == "'a b'" and shell_quote("a'b", "single") == "a'\\''b"


@pytest.mark.parametrize("value", HARD)
def test_codex_argv_and_the_script_after_bash_c(value):
    v, ph = vault_with(value)
    out = restore_tool("shell", {"command": ["bash", "-lc", f"printf '%s' {ph}"]}, v, DEFAULT_POLICY, Counter())
    assert run(out["command"][2]) == value
    out = restore_tool("shell", {"command": ["printf", "%s", ph]}, v, DEFAULT_POLICY, Counter())
    assert out["command"][2] == value  # an argument on its own: no shell reads it


@pytest.mark.parametrize("value", HARD)
def test_a_code_mode_script_gets_the_value_in_its_literals(value):
    """Codex code mode: in a patch, the file gets the value; in a command, the shell does."""
    v, ph = vault_with(value)
    patch = f"*** Begin Patch\n*** Add File: .env\n+API_SECRET={ph}\n*** End Patch\n"
    script = (f"await tools.apply_patch({json.dumps(patch)});\n"
              f"await tools.exec_command({{cmd: {json.dumps(f'echo \"PW={ph}\" >> .env')}}});\n"
              f"await tools.exec_command({{cmd: `printf %s {ph}`}});")
    out = restore_tool("exec", script, v, DEFAULT_POLICY, Counter())
    node = subprocess.run(["node", "-e", "const tools = {apply_patch: s => console.log(JSON.stringify(s)), "
                           "exec_command: a => console.log(JSON.stringify(a.cmd))};\n(async () => {" + out + "})()"],
                          capture_output=True, text=True, timeout=10)
    if node.returncode == 127 or "not found" in node.stderr:
        pytest.skip("node is not installed")
    patch_seen, echo_cmd, printf_cmd = [json.loads(line) for line in node.stdout.splitlines()]
    assert patch_seen == patch.replace(ph, value)
    assert run(echo_cmd.replace(">> .env", "")).rstrip("\n") == f"PW={value}"
    assert run(printf_cmd) == value


@pytest.mark.parametrize("value", HARD)
def test_a_json_file_being_written_stays_valid(value):
    v, ph = vault_with(value)
    content = json.dumps({"password": ph, "host": "db"}, indent=2)
    out = restore_tool("Write", {"file_path": "/x/c.json", "content": content}, v, DEFAULT_POLICY, Counter())
    assert json.loads(out["content"]) == {"password": value, "host": "db"}
    assert out["content"].count("\n") == content.count("\n")  # formatting kept


def test_one_value_one_placeholder_however_it_was_escaped():
    """A password read plain (Claude) and inside JSON text (Codex) is one value, one placeholder."""
    r = Redactor(Pipeline(RulesDetector()), Vault(bytes(range(32))))
    plain = r.redact_text('ADMIN_PASSWORD=Tr0ub4dor"&\\3x')
    escaped = r.redact_text(json.dumps({"output": 'ADMIN_PASSWORD=Tr0ub4dor"&\\3x\nX=1\n'}))
    ph = plain.split("=", 1)[1]
    assert ph in escaped and r.vault.value(ph) == 'Tr0ub4dor"&\\3x'
    assert json.loads(restore_text(escaped, r.vault))["output"] == 'ADMIN_PASSWORD=Tr0ub4dor"&\\3x\nX=1\n'


def test_a_respelled_placeholder_is_put_back():
    """Models slugify: `keepmum-secret-…` in a file name is that placeholder, and gets its value."""
    v, ph = vault_with("ghp_" + "a1B2c3D4" * 4 + "Zz9q")
    token = v.value(ph)
    for spelled in (ph.lower(), ph.lower().replace("_", "-"), ph.upper(), "Keepmum_Secret_" + ph[15:]):
        out = restore_tool("Bash", {"command": f"touch notes-{spelled}.md"}, v, DEFAULT_POLICY, Counter())
        assert out["command"] == f"touch notes-{token}.md", spelled
        assert restore_text(f"saved notes-{spelled}.md", v) == f"saved notes-{token}.md"
    # Spelled like a placeholder this vault never made: left alone.
    assert restore_text("keepmum-secret-0011aabb", v) == "keepmum-secret-0011aabb"


def test_a_mistyped_placeholder_stops_the_command():
    """One digit off: run, the command would create `KEEPMUM_SECRET_….txt`. It is not run, and the
    model is told which placeholder it probably meant."""
    v, ph = vault_with("ghp_" + "a1B2c3D4" * 4 + "Zz9q")
    typo = ph[:-1] + ("0" if ph[-1] != "0" else "1")
    counts = Counter()
    out = restore_tool("Bash", {"command": f"cp backup-{ph}.txt {typo}.txt", "description": "copy"}, v,
                       DEFAULT_POLICY, counts)
    assert "cp " not in out["command"] and "exit 1" in out["command"] and ph in out["command"]
    assert out["description"] == "copy" and counts["unresolved"] == 1
    assert v.value(ph) not in out["command"]
    script = f"await tools.exec_command({{cmd: 'mkdir {typo}'}});"
    assert restore_tool("exec", script, v, DEFAULT_POLICY, Counter()).startswith("throw new Error(")
    # Far from any placeholder of this conversation (keepmum's own test data): run as written.
    other = "KEEPMUM_SECRET_0011aabb"
    assert restore_tool("Bash", {"command": f"grep {other} tests/"}, v, DEFAULT_POLICY, Counter())["command"] == f"grep {other} tests/"
    # A file being written is not stopped: it may hold placeholder-shaped text on purpose.
    assert restore_tool("Write", {"file_path": "/x", "content": typo}, v, DEFAULT_POLICY, Counter())["content"] == typo


# --- What comes back: a command keepmum restored, in the agent's history -----------------------

@pytest.mark.parametrize("value", ['Rv7$Lq2"Nw8$Tz4', "p@ss w0rd 'x'", "c0rrect\\horse`9Zq"])
@pytest.mark.parametrize("template", ['echo "DB_PASSWORD={}" >> .env', "echo 'DB_PASSWORD={}' >> .env",
                                      "echo DB_PASSWORD={} >> .env", "cat <<EOF >> .env\nDB_PASSWORD={}\nEOF"])
def test_a_restored_command_comes_back_with_the_placeholder_the_model_wrote(value, template):
    """keepmum puts a value back escaped for the quotes around it. The agent sends the command it ran
    back in its history: escaped, the value was none of the text keepmum knew, so it went to the
    provider as it read (`'p@ss w0rd '\\''x'\\''`), or was masked anew under a placeholder the model
    never wrote, and a model retried "its typo" until it made one up (a live Codex run, val-04)."""
    import json
    from collections import Counter

    from keepmum.detect.pipeline import Pipeline
    from keepmum.detect.rules import RulesDetector
    from keepmum.redact import Redactor
    from keepmum.restore import DEFAULT_POLICY, restore_tool

    vault = Vault(bytes(range(32)))
    ph = vault.placeholder("SECRET", value, ["rules:secret-assignment"])
    wrote = template.format(ph)
    ran = restore_tool("exec_command", {"cmd": wrote}, vault, DEFAULT_POLICY, Counter())["cmd"]
    r = Redactor(Pipeline(RulesDetector()), vault)
    # Codex (a function call's arguments) and Claude Code (a tool_use input).
    codex = r.redact_responses_request({"input": [
        {"type": "function_call", "call_id": "c1", "name": "exec_command", "arguments": json.dumps({"cmd": ran})}]})
    sent = next(i for i in codex["input"] if i.get("type") == "function_call")
    assert json.loads(sent["arguments"])["cmd"] == wrote
    claude = r.redact_request({"messages": [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": ran}}]}]})
    assert claude["messages"][0]["content"][0]["input"]["command"] == wrote


@pytest.mark.parametrize("template", [
    "bash -lc 'cat clients/{ph}_cover.md'", 'bash -lc "cat clients/{ph}_cover.md"',
    "sh -c 'echo DB_PASSWORD={pw} > out.txt'", "bash -euo pipefail -c 'echo {pw} > out.txt'",
    "/bin/zsh -lc 'echo \"{pw}\" > out.txt'"])
def test_a_value_in_a_script_a_shell_runs_is_quoted_for_that_shell_too(template, tmp_path):
    """`bash -lc '…'`: the quotes are the outer shell's, the script is the inner one's. Escaped only
    for the outer quotes, a name with a space was split in two by the inner shell (its halves went
    out in error messages) and a password's `$…` expanded (.env got `Xk9`): a live run, gpt-5-nano."""
    import shutil
    import subprocess
    from collections import Counter

    from keepmum.restore import DEFAULT_POLICY, restore_tool

    if template.startswith("/bin/zsh") and not shutil.which("zsh"):
        pytest.skip("no zsh")
    v = Vault(bytes(range(32)))
    name, pw = "Margaret Thornbury", 'Rv7$Lq2"Nw8$Tz4'
    cmd = template.format(ph=v.placeholder("PERSON", name), pw=v.placeholder("SECRET", pw))
    (tmp_path / "clients").mkdir()
    (tmp_path / "clients" / f"{name}_cover.md").write_text("hi\n")
    ran = restore_tool("exec_command", {"cmd": cmd}, v, DEFAULT_POLICY, Counter())["cmd"]
    r = subprocess.run(["bash", "-c", ran], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, (ran, r.stderr)
    if "{pw}" in template:
        assert (tmp_path / "out.txt").read_text().strip() in (pw, f"DB_PASSWORD={pw}")
    else:
        assert r.stdout == "hi\n"
