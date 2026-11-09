"""What must be true after every run, whatever the case asked, plus the case's own expectations.

Every finding has a class, worst first:

  leak        a protected value left this Mac (any form: raw, JSON-escaped, base64, URL-encoded)
  residue     a placeholder ended up where the user's real value belongs: a file name or file on
              disk, a command the agent ran, the answer the user reads
  false-mask  a file name or path the workspace holds was sent masked, or a command's own syntax
              (`&&`, ` > `, ` | `): the model saw a placeholder for something never sensitive
  task        the workspace is not what the case expects: a file missing, a wrong name, wrong text
  health      keepmum or the agent misbehaved: unrestorable placeholders, errors, a script not played
  shown       a placeholder in something the user sees but that changes nothing (reasoning summaries)
  unexercised live only: a value the case says is masked never reached the model at all, in any form
              (it copied the value with a command without reading it); the run tests less than meant

leak, residue, false-mask, task and health fail a run; shown and unexercised are reported only.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
from dataclasses import asdict, dataclass
from fnmatch import fnmatch
from pathlib import Path

from keepmum.detect import TYPES
from keepmum.vault import PLACEHOLDER, Vault

from .agents import Transcript
from .case import Case
from .servers import Exchange

FAILING = ("leak", "residue", "false-mask", "task", "health")
# A placeholder in any spelling a model might give it: lowered, slugged, shortened.
RESIDUE = re.compile(r"(?i)keepmum[_-](?:" + "|".join(TYPES) + r")[_-]?[0-9a-f]{4,12}")
# Shell syntax no secret holds: masked, the model can no longer read its own command.
SHELL_SYNTAX = re.compile(r" (?:&&|\|\||[|><]|>>|2>&1) ")
_SPACE = re.compile(r"\s+")
# What agents leave in a workspace that no case is about.
AGENT_FILES = (".git/*", ".codex/*", ".claude/*", "*.DS_Store")


@dataclass
class Finding:
    cls: str
    what: str
    where: str = ""

    def failing(self) -> bool:
        return self.cls in FAILING


def snapshot(ws: Path) -> dict[str, bytes]:
    out = {}
    for p in sorted(ws.rglob("*")):
        rel = p.relative_to(ws).as_posix()
        if p.is_file() and not any(fnmatch(rel, g) for g in AGENT_FILES):
            out[rel] = p.read_bytes()
    return out


def forms(value: str) -> list[bytes]:
    """A value as it could appear in a request body: as is, JSON-escaped once or twice (Codex sends
    tool output as JSON inside a JSON string), base64, URL-encoded."""
    once = json.dumps(value, ensure_ascii=False)[1:-1]
    twice = json.dumps(once, ensure_ascii=False)[1:-1]
    return list(dict.fromkeys([value.encode(), json.dumps(value)[1:-1].encode(), once.encode(), twice.encode(),
                               base64.b64encode(value.encode()), urllib.parse.quote(value).encode(),
                               urllib.parse.quote_plus(value).encode()]))


def judge(case: Case, salt: bytes, exchanges: list[Exchange], before: dict[str, bytes], after: dict[str, bytes],
          transcript: Transcript, masked: dict[str, tuple[str, str]], keepmum: dict, exit_code: int | None,
          served: list[dict] | None, mcp_log: str | None = None) -> list[Finding]:
    """`masked`: placeholder -> (type, value) for every value keepmum masked in the run (its vault).
    `mcp_log`: what the stub MCP server received (case.mcp), one JSON line per call."""
    out: list[Finding] = []
    sent = [(i, ex) for i, ex in enumerate(exchanges)]

    # leak: no form of a protected value in anything that left.
    for v in case.values.values():
        if not v.must_mask:
            continue
        for i, ex in sent:
            blob = ex.sent()
            if any(f in blob for f in forms(v.text) if len(f) >= 4):
                out.append(Finding("leak", f"{{{{v:{v.name}}}}} reached the provider", f"request {i}: {ex.method} {ex.path}"))
                break

    # leak, encoded: a protected value in a hex dump, `od -c`, plain hex or base64 that left (a
    # command printed the file that way: keepmum.decoded).
    from keepmum.decoded import blocks

    for i, ex in sent:
        decoded = "".join(d for s in _strings(ex.body) for _, _, d in blocks(s))
        for v in case.values.values():
            if v.must_mask and len(v.text) >= 4 and v.text in decoded:
                out.append(Finding("leak", f"{{{{v:{v.name}}}}} reached the provider encoded (a hex dump or base64)",
                                   f"request {i}: {ex.method} {ex.path}"))

    # leak, in pieces: a secret cut up and spread out (a dump's text column, spaced characters, hex
    # bytes with spaces between them), found without decoding anything: every whitespace removed
    # from what was sent, then any 12 characters of the secret, or 24 digits of its hex.
    for v in case.values.values():
        if not v.must_mask or v.kind != "SECRET" or len(v.text) < 24:
            continue
        hexed = v.text.encode().hex()
        pieces = {v.text[i:i + 12] for i in range(len(v.text) - 11)} | {hexed[i:i + 24] for i in range(0, len(hexed) - 23, 2)}
        for i, ex in sent:
            squeezed = "".join(_SPACE.split("".join(_strings(ex.body))))
            if any(p in squeezed for p in pieces):
                out.append(Finding("leak", f"pieces of {{{{v:{v.name}}}}} reached the provider", f"request {i}: {ex.method} {ex.path}"))
                break

    # leak, in part: a protected value masked only in part (up to a quote, a space), the rest sent.
    sent_all = b"".join(ex.sent() for _, ex in sent)
    for v in case.values.values():
        if not v.must_mask:
            continue
        parts = [u for _, (_, u) in masked.items() if u != v.text and u in v.text and len(u) >= 3]
        if parts:
            rest = v.text
            for u in sorted(parts, key=len, reverse=True):
                rest = rest.replace(u, "\0")
            sent_rest = [r for r in rest.split("\0") if len(r.strip()) >= 3 and any(f in sent_all for f in forms(r))]
            if sent_rest:
                out.append(Finding("leak", f"{{{{v:{v.name}}}}} was masked only in part; {len(''.join(sent_rest))} of its "
                                           f"{len(v.text)} characters were sent", "requests"))

    # leak, through a connector: what the stub MCP server received would have left the Mac, and
    # what the agent called it with (a call that failed or was cancelled shows it too).
    for inp in transcript.mcp_inputs:
        got = inp.encode()
        for v in case.values.values():
            if v.must_mask and v.name not in case.expect.get("mcp_may_receive", []) and \
                    any(f in got for f in forms(v.text) if len(f) >= 4):
                out.append(Finding("leak", f"the agent called an MCP tool with {{{{v:{v.name}}}}}", "mcp"))
    if mcp_log is not None:
        may = set(case.expect.get("mcp_may_receive", []))
        got = mcp_log.encode()
        for v in case.values.values():
            if v.must_mask and v.name not in may and any(f in got for f in forms(v.text) if len(f) >= 4):
                out.append(Finding("leak", f"{{{{v:{v.name}}}}} reached the MCP server", "mcp"))
        calls = sum(1 for line in mcp_log.splitlines() if line.strip())
        if calls < case.expect.get("mcp_calls_min", 0):
            out.append(Finding("task", f"the MCP server got {calls} call(s), not {case.expect['mcp_calls_min']}", "mcp"))
        for want in case.expect.get("mcp_contains", []):
            want = case.resolve(want, salt)
            if not any(f in got for f in forms(want)):
                out.append(Finding("task", f"the MCP server never received {case.shown(want)!r}", "mcp"))

    # leak, through a tool that runs off this Mac: a web search, a web fetch, a browser.
    for inp in transcript.network_inputs:
        for v in case.values.values():
            if v.must_mask and any(f in inp.encode() for f in forms(v.text) if len(f) >= 4):
                out.append(Finding("leak", f"the agent sent {{{{v:{v.name}}}}} to a web tool", "network"))
    if transcript.web_calls < case.expect.get("web_calls_min", 0):
        out.append(Finding("task", f"the agent used a web tool {transcript.web_calls} time(s), not "
                                   f"{case.expect['web_calls_min']}", "network"))

    # residue: no placeholder in any spelling where a real value belongs. A case names the files
    # where a placeholder is right (expect.residue_ok): what a command sent to a wrong host.
    ok = [case.resolve(g, salt) for g in case.expect.get("residue_ok", [])]
    for rel, data in after.items():
        if any(fnmatch(rel, g) for g in ok):
            continue
        if RESIDUE.search(rel):
            out.append(Finding("residue", f"a file name holds a placeholder: {case.shown(rel)}", "disk"))
        text = data.decode("utf-8", "replace")
        if m := RESIDUE.search(text):
            out.append(Finding("residue", f"{case.shown(rel)} holds a placeholder: …{case.shown(_around(text, m))}…", "disk"))
    for cmd in transcript.commands:
        if case.expect.get("commands_may_keep"):
            break
        if m := RESIDUE.search(cmd):
            out.append(Finding("residue", f"the agent ran a command with a placeholder: …{case.shown(_around(cmd, m))}…", "command"))
    for inp in transcript.tool_inputs:
        if case.expect.get("commands_may_keep"):
            break
        if m := RESIDUE.search(inp):
            out.append(Finding("residue", f"a tool ran with a placeholder in its input: …{case.shown(_around(inp, m))}…", "tool"))
    if m := RESIDUE.search(transcript.final):
        out.append(Finding("residue", f"the answer shows a placeholder: …{case.shown(_around(transcript.final, m))}…", "answer"))
    for text in transcript.shown:
        if text is not transcript.final and (m := RESIDUE.search(text)):
            out.append(Finding("shown", f"the user sees a placeholder: …{case.shown(_around(text, m))}…", "transcript"))
            break

    # false-mask: names and paths in the workspace, and the case's plain strings, go as they are.
    declared = {v.text for v in case.values.values()}
    paths = set(before) | set(after)
    pieces = {p for rel in paths for p in [rel, *rel.split("/")]}
    for ph, (kind, value) in masked.items():
        if value in declared:
            continue
        if value in pieces or any(value in rel and _is_path_piece(value, rel) for rel in paths):
            out.append(Finding("false-mask", f"workspace path {value!r} was sent as a {kind} placeholder", ph))
        elif not any(v in value for v in declared) and SHELL_SYNTAX.search(value):
            out.append(Finding("false-mask", f"a command's own text {value!r} was sent as a {kind} placeholder", ph))
    vault = Vault(salt)
    sent_text = b"".join(ex.sent() for _, ex in sent).decode("utf-8", "replace")
    for s in case.plain:
        s = case.resolve(s, salt)
        for kind in TYPES:
            ph = vault.placeholder(kind, s)
            if ph in sent_text:
                out.append(Finding("false-mask", f"{s!r} was sent as a {kind} placeholder", ph))

    # A protected value masked together with the text around it: the model cannot see that text,
    # and copies a placeholder that restores more (or less) than the value.
    wide = set()
    for v in case.values.values():
        for ph, (kind, value) in masked.items():
            if value != v.text and v.text in value:
                wide.add(v.name)
                out.append(Finding("false-mask", f"{{{{v:{v.name}}}}} was masked with the text around it, as one "
                                                 f"{kind}: {case.shown(value)[:120]!r}", ph))
                break

    # The case's own placeholders: those it says are masked reach the provider as placeholders.
    for name in case.expect.get("masked", []):
        ph = case.placeholder(name, salt)
        if ph not in sent_text and name not in wide:
            text = case.values[name].text
            cls = "unexercised" if served is None and text not in sent_text else "task"
            out.append(Finding(cls, f"{{{{v:{name}}}}} never reached the provider as its placeholder {ph}", "requests"))

    # task: the workspace the case expects.
    out += _files(case, salt, before, after)
    for want in case.expect.get("answer_contains", []):
        if case.resolve(want, salt) not in transcript.final:
            out.append(Finding("task", f"the answer does not say {case.shown(case.resolve(want, salt))!r}", "answer"))

    # health
    if exit_code not in (0, None) and not case.expect.get("agent_may_fail"):
        out.append(Finding("health", f"the agent exited {exit_code}", "agent"))
    if exit_code is None:
        out.append(Finding("health", f"the agent did not finish in {case.timeout} s", "agent"))
    # A check that warns (lets the request through): keepmum recorded it, and the user saw its note.
    for check in case.expect.get("warned", []):
        if check not in keepmum.get("warned", []):
            out.append(Finding("task", f"keepmum's {check} check never warned", "keepmum"))
    if case.expect.get("warn_shown") and not any("keepmum: " in t for t in [*transcript.shown, transcript.final]):
        out.append(Finding("task", "the agent never showed keepmum's note", "transcript"))
    if keepmum.get("unresolved") and keepmum["unresolved"] != case.expect.get("unresolved"):
        out.append(Finding("health", f"keepmum met {keepmum['unresolved']} placeholder(s) it could not restore", "keepmum"))
    for e in keepmum.get("errors", []):
        # Claude Code probes {base}/api/hello before a session; a gateway that is not Anthropic
        # (DeepSeek's) answers it 401 and Claude Code carries on: nothing failed.
        if e["path"].endswith("/api/hello") and e["outcome"] == "forwarded":
            continue
        out.append(Finding("health", f"keepmum answered {e['status']} ({e['outcome']}) for {e['path']}", "keepmum"))
    if served is not None and case.script:
        last = max((s["turn"] for s in served), default=-1)
        if last < len(case.script) - 1:
            out.append(Finding("health", f"the script stopped at turn {last + 1} of {len(case.script)}: "
                                         "the agent ended early or a tool call failed", "script"))
    for err in transcript.errors[:3]:
        out.append(Finding("health", f"agent error: {case.shown(err)[:300]}", "agent"))
    return out


def _strings(body: bytes) -> list[str]:
    """Every string in a request body, a JSON document in one unpacked too (Codex's tool output is
    JSON inside a JSON string), as keepmum reads them; the raw text when the body is not JSON."""
    text = body.decode("utf-8", "replace")
    try:
        todo, out = [json.loads(text)], []
    except ValueError:
        return [text]
    while todo:
        x = todo.pop()
        if isinstance(x, dict):
            todo += x.values()
        elif isinstance(x, list):
            todo += x
        elif isinstance(x, str):
            out.append(x)
            if x[:1] in "{[":
                try:
                    todo.append(json.loads(x))
                except ValueError:
                    pass
    return out


def _is_path_piece(value: str, rel: str) -> bool:
    """value sits in rel between separators: a name, a stem, a dated part of a name."""
    return bool(re.search(rf"(?:^|[/._-]){re.escape(value)}(?:$|[/._-])", rel))


def _around(text: str, m: re.Match, n: int = 40) -> str:
    return text[max(0, m.start() - n): m.end() + n].replace("\n", "⏎")


def _files(case: Case, salt: bytes, before: dict[str, bytes], after: dict[str, bytes]) -> list[Finding]:
    out = []
    exp = case.expect
    for rel_t, want in (exp.get("files") or {}).items():
        rel = case.resolve(rel_t, salt)
        if rel not in after:
            near = [case.shown(p) for p in after if p not in before]
            out.append(Finding("task", f"missing file {case.shown(rel)!r}" + (f"; new files: {near}" if near else ""), "disk"))
            continue
        text = after[rel].decode("utf-8", "replace")
        if isinstance(want, str):
            want = {"equals": want}
        if "equals" in want and text != case.resolve(want["equals"], salt):
            out.append(Finding("task", f"{case.shown(rel)} is {case.shown(text)!r}, expected "
                                       f"{case.shown(case.resolve(want['equals'], salt))!r}", "disk"))
        for piece in _list(want.get("contains")):
            if case.resolve(piece, salt) not in text:
                out.append(Finding("task", f"{case.shown(rel)} does not contain {piece!r}", "disk"))
        for piece in _list(want.get("lacks")):
            if case.resolve(piece, salt) in text:
                out.append(Finding("task", f"{case.shown(rel)} contains {piece!r}", "disk"))
        if "regex" in want and not re.search(case.resolve(want["regex"], salt), text):
            out.append(Finding("task", f"{case.shown(rel)} does not match {want['regex']!r}", "disk"))
    for rel_t in exp.get("absent", []):
        rel = case.resolve(rel_t, salt)
        if rel in after:
            out.append(Finding("task", f"{case.shown(rel)!r} should not exist", "disk"))
    if "only_new" in exp:
        allowed = [case.resolve(p, salt) for p in exp["only_new"]]
        for rel in after:
            if rel not in before and not any(fnmatch(rel, a) for a in allowed):
                out.append(Finding("task", f"unexpected new file {case.shown(rel)!r}", "disk"))
    if exp.get("others_unchanged"):
        named = {case.resolve(p, salt) for p in [*(exp.get("files") or {}), *exp.get("absent", [])]}
        for rel, data in before.items():
            if rel not in named and after.get(rel) != data:
                out.append(Finding("task", f"{case.shown(rel)!r} was " + ("removed" if rel not in after else "changed"), "disk"))
    return out


def _list(x) -> list:
    return [] if x is None else [x] if isinstance(x, str) else list(x)


def as_dicts(findings: list[Finding]) -> list[dict]:
    return [asdict(f) for f in findings]


def placeholders_in(exchanges: list[Exchange]) -> set[str]:
    return {m.group(0) for ex in exchanges for m in PLACEHOLDER.finditer(ex.sent().decode("utf-8", "replace"))}
