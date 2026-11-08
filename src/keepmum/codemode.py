"""Codex's code mode: the model's tool calls are a JavaScript program for the `exec` tool.

Since Codex 0.147 a model does not call `exec_command`, `apply_patch` or an MCP server's tools
itself. It writes a script that does (`await tools.exec_command({cmd: "curl …"})`,
`await tools.mcp__slack__post({text: …})`), and Codex runs it in a V8 isolate that has no network
of its own: the script reaches the network only through the nested tools. So a value can leave
only through a nested tool's input, and the values a script holds are in its string literals.

keepmum reads a script's literals and the nested tool each one is passed to (`parse`):

- a literal passed straight to a call (`tools.NAME(… "…" …)`, with no other call in between) is
  that tool's input, held to that tool's policy (restore.py): an MCP server's literal gets only
  what the server may receive, a command's literal is a command (destinations.py, commands.py), a
  patch's literal is a file's new text;
- any other literal (a variable, an argument to the script's own function) can end up in any
  tool the script calls, so it gets only what every network tool the script calls may receive;
- a script that reaches tools in a way that cannot be read (`tools[name]`, `eval`, `globalThis`…)
  may call any tool: such a literal gets nothing.

What a script does with a tool's output is not seen: `tools.mcp__x__send({text: (await
tools.exec_command({cmd: "cat .env"})).output})` sends a file's real text, which never passes
keepmum. That is the same for a shell command (`curl -d @.env …`); keepmum decides which real
values the model's own text may carry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Tools whose input is a code-mode script, when it is a string.
CODE_TOOLS = frozenset({"exec"})
# Nested tools that run a command: their literals are shell text.
SHELL_CALLS = frozenset({"exec_command", "write_stdin"})
# Nested tools that reach an MCP server named in their arguments.
MCP_RESOURCE_CALLS = frozenset({"read_mcp_resource", "list_mcp_resources", "list_mcp_resource_templates"})

_ESCAPE = re.compile(r"\\(u\{[0-9a-fA-F]+\}|u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|\r\n|[\s\S])")
_SIMPLE = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0", "\n": "", "\r\n": ""}
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
# tools.NAME, tools?.NAME, tools["NAME"], tools?.["NAME"]
_TOOL_REF = re.compile(r"tools\s*(?:(?:\?\.|\.)\s*([A-Za-z_$][\w$]*)|(?:\?\.)?\s*\[\s*([\"'`])([\w$-]+)\2\s*\])")
_CALL_OPEN = re.compile(r"\s*(?:\?\.\s*)?\(")
# Names through which a script can reach a tool without naming it where it calls it.
_DYNAMIC = frozenset({"eval", "Function", "globalThis", "Reflect", "constructor", "import", "require", "self", "window",
                      "with"})
_BOUND = re.compile(r"\s*=\s*[\"'`]")  # `= "`: the literal starts after the quote
_UNPACK = re.compile(r"\b(?:const|let|var)\s*([\[{][^=]*)=")
_AFTER_BOUND = re.compile(r"[ \t]*(?:;|\n|$|,)")
_KEYWORDS = frozenset({"const", "let", "var", "await", "async", "function", "return", "if", "else", "for", "of", "in",
                       "while", "new", "typeof", "true", "false", "null", "undefined", "try", "catch", "finally",
                       "throw", "break", "continue", "switch", "case", "default", "do", "void", "delete",
                       "instanceof", "yield", "class", "this", "super"})
_SERVER_ARG = re.compile(r"\bserver\s*:\s*([\"'`])([\w.-]+)\1")
# After these, a `/` starts a regular expression, not a division.
_REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^") | {""}
_REGEX_KEYWORDS = frozenset({"return", "typeof", "case", "do", "else", "in", "of", "void", "yield", "await", "delete",
                             "instanceof", "new", "throw"})


def _unescape(raw: str) -> str:
    def one(m):
        e = m.group(1)
        if e.startswith("u{"):
            return chr(int(e[2:-1], 16))
        if e[0] in "ux" and len(e) > 1:
            return chr(int(e[1:], 16))
        return _SIMPLE.get(e, e)

    return _ESCAPE.sub(one, raw)


@dataclass(frozen=True)
class Literal:
    start: int  # the literal's text, between its quotes (a template's: one chunk between ${…} parts)
    end: int
    quote: str
    call: str | None  # the nested tool it is passed straight to, else None
    span: tuple[int, int]  # the whole literal: for a template, from its first chunk to its last
    call_span: tuple[int, int] | None = None  # the call's arguments, from its ( to its )

    @property
    def in_patch(self) -> bool:
        return self.call == "apply_patch"

    @property
    def shell(self) -> bool:
        return self.call in SHELL_CALLS


@dataclass(frozen=True)
class Ref:
    """A name used in a script's code (not a property: `x.name`, not a key: `{name: …}`)."""
    start: int
    end: int
    name: str
    call: str | None  # the nested tool it is passed straight to, else None
    call_span: tuple[int, int] | None
    shorthand: bool  # `{name}`: the key and the value


@dataclass
class Script:
    literals: list[Literal]
    calls: set[str]  # every nested tool the script names
    dynamic: bool  # it can reach a tool without naming it
    refs: list[Ref] = field(default_factory=list)
    declared: dict[str, list[int]] = field(default_factory=dict)  # name -> where each const/let/var of it is
    assigned: set[str] = field(default_factory=set)  # names assigned after their declaration, or parameters
    call_spans: list[tuple[str, int, int]] = field(default_factory=list)  # (nested tool, its "(", its ")")

    def bindings(self, source: str) -> dict[str, Literal]:
        """Names bound once to a plain string literal and never changed (`const tok = "…";`):
        where one is passed to a tool, it is that literal."""
        out = {}
        by_start = {lit.start: lit for lit in self.literals}
        # Names a destructuring declaration binds: `const {a, b: tok} = …`
        unpacked = {w for m in _UNPACK.finditer(source) for w in _IDENT.findall(m.group(1))}
        for name, at in self.declared.items():
            if len(at) != 1 or name in self.assigned or name in unpacked:
                continue
            m = _BOUND.match(source, at[0] + len(name))
            lit = by_start.get(m.end()) if m else None
            if lit is None or lit.span != (lit.start, lit.end) or not _AFTER_BOUND.match(source, lit.end + 1):
                continue
            out[name] = lit
        return out

    def tool_name(self, lit: Literal, source: str) -> str | None:
        """The name keepmum decides a literal's call by: an MCP resource call is its server's."""
        if lit.call in MCP_RESOURCE_CALLS:
            a, b = lit.call_span or (0, 0)
            m = _SERVER_ARG.search(source, a, b)
            return f"mcp__{m.group(2) if m else '?'}__{lit.call}"
        return lit.call

    def network(self, source: str, action_of) -> set[str]:
        """The tools the script names whose policy is not "restore" (`action_of(name)`)."""
        names = set()
        for name in self.calls:
            if name in MCP_RESOURCE_CALLS:
                servers = {self.tool_name(lit, source) for lit in self.literals if lit.call == name} or \
                    {f"mcp__?__{name}"}
                names |= servers
            else:
                names.add(name)
        return {n for n in names if action_of(n) != "restore"}


def parse(source: str) -> Script:
    """A script's literals, each with the nested tool it is passed to, and the tools it names."""
    lits: list[Literal] = []
    calls: set[str] = set()
    dynamic = False
    refs: list[tuple] = []
    declared: dict[str, list[int]] = {}
    assigned: set[str] = set()
    # Open brackets: ("(", tool or None, where it opened), ("{", …), ("${", template index)
    stack: list[tuple[str, str | None, int]] = []
    templates: list[list] = []  # per open template: [span start, chunks [(a, b, call, call_open)]]
    closed: dict[int, int] = {}  # a call's ( -> its )
    spans: list[tuple[str, int, int]] = []  # every closed nested tool call: (tool, its "(", its ")")
    prev = ""  # the last significant token, to tell a regular expression from a division
    i, n = 0, len(source)

    def current() -> tuple[str | None, int | None]:
        for kind, name, at in reversed(stack):
            if kind == "(":
                return name, at
        return None, None

    def template(i: int) -> int:
        """Read template text from i to its closing ` or a ${; returns where code resumes."""
        j = i
        while j < n:
            if source[j] == "\\":
                j += 2
                continue
            if source[j] == "`":
                break
            if source.startswith("${", j):
                templates[-1][1].append((i, j, *current()))
                stack.append(("${", None, len(templates) - 1))
                return j + 2
            j += 1
        span_start, chunks = templates.pop()
        chunks.append((i, min(j, n), *current()))
        for a, b, call, at in chunks:
            lits.append(Literal(a, b, "`", call, (span_start, min(j, n)), (at, -1) if at is not None else None))
        return j + 1

    while i < n:
        c = source[i]
        if c in " \t\r\n":
            i += 1
            continue
        if source.startswith("//", i):
            j = source.find("\n", i)
            i = n if j < 0 else j
            continue
        if source.startswith("/*", i):
            j = source.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "/" and (prev in _REGEX_BEFORE or prev in _REGEX_KEYWORDS):
            j, in_class = i + 1, False
            while j < n and source[j] != "\n":
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == "[":
                    in_class = True
                elif source[j] == "]":
                    in_class = False
                elif source[j] == "/" and not in_class:
                    break
                j += 1
            i, prev = j + 1, "regex"
            while i < n and source[i].isalpha():
                i += 1
            continue
        if c in "'\"":
            j = i + 1
            while j < n and source[j] != c:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == "\n":  # an unterminated literal ends at the line
                    break
                j += 1
            call, at = current()
            lits.append(Literal(i + 1, min(j, n), c, call, (i + 1, min(j, n)), (at, -1) if at is not None else None))
            i, prev = j + 1, "string"
            continue
        if c == "`":
            templates.append([i + 1, []])
            i, prev = template(i + 1), "string"
            continue
        if c == "}" and stack and stack[-1][0] == "${":
            stack.pop()
            i, prev = template(i + 1), "string"
            continue
        if (m := _TOOL_REF.match(source, i)) and not (i and (source[i - 1].isalnum() or source[i - 1] in "_$")):
            name = m.group(1) or m.group(3)
            calls.add(name)
            if (o := _CALL_OPEN.match(source, m.end())) is not None:
                stack.append(("(", name, o.end() - 1))
                i, prev = o.end(), "("
            else:
                i, prev = m.end(), "name"
            continue
        if (m := _IDENT.match(source, i)) is not None:
            word = m.group()
            # A property named `tools` is not the global (`x.tools`); `.constructor` reaches Function.
            if word == "constructor" or ((word == "tools" or word in _DYNAMIC) and not (i and source[i - 1] == ".")):
                dynamic = True
            if word == "apply_patch" and (o := _CALL_OPEN.match(source, m.end())) is not None:
                calls.add("apply_patch")
                stack.append(("(", "apply_patch", o.end() - 1))
                i, prev = o.end(), "("
                continue
            if word not in _KEYWORDS and not (i and source[i - 1] == "."):
                after = source[m.end():m.end() + 8].lstrip()
                in_object = bool(stack) and stack[-1][0] == "{" and prev in ("{", ",")
                if prev in ("const", "let", "var"):
                    declared.setdefault(word, []).append(i)
                elif after.startswith("=>") or (stack and stack[-1][0] == "(" and stack[-1][1] is None
                                                and re.match(r"[^()]*\)\s*=>", source[m.end():])):
                    assigned.add(word)  # an arrow function's parameter: another binding of the name
                elif re.match(r"(?:[-+*/%&|^]|\*\*|<<|>>>?|&&|\|\||\?\?)?=(?![=>])|\+\+|--", after) or \
                        re.search(r"(?:\+\+|--)\s*$", source[max(0, i - 4):i]):
                    assigned.add(word)
                elif stack and stack[-1][0] == "(" and stack[-1][1] is None and \
                        re.search(r"(?:\bfunction\b\s*[\w$]*|\bcatch)\s*$", source[:stack[-1][2]]):
                    assigned.add(word)  # a function's parameter: another binding of the name
                elif in_object and after[:1] == ":":
                    pass  # a key
                else:
                    shorthand = in_object and after[:1] in ("}", ",")
                    call, at = current()
                    refs.append((i, m.end(), word, call, at, shorthand))
            if prev in ("function", "class"):
                assigned.add(word)  # a function's name
            i, prev = m.end(), word
            continue
        if c == "(":
            stack.append(("(", None, i))
        elif c in "{[":
            stack.append((c, None, i))
        elif c in ")}]":
            if stack and stack[-1][0] != "${":
                kind, name, at = stack.pop()
                if kind == "(" and name is not None:
                    closed[at] = i
                    spans.append((name, at, i))
        i, prev = i + 1, c
    while templates:  # unterminated
        span_start, chunks = templates.pop()
        for a, b, call, at in chunks:
            lits.append(Literal(a, b, "`", call, (span_start, n), (at, -1) if at is not None else None))
    out = []
    for lit in sorted(lits, key=lambda x: x.start):
        if lit.call_span is not None:
            at = lit.call_span[0]
            lit = Literal(lit.start, lit.end, lit.quote, lit.call, lit.span, (at, closed.get(at, n)))
        out.append(lit)
    names = [Ref(a, b, w, call, (at, closed.get(at, n)) if at is not None else None, sh)
             for a, b, w, call, at, sh in refs]
    return Script(out, calls, dynamic, names, declared, assigned, spans)


def literals(source: str) -> list[Literal]:
    """The string and template literals of a script (comments skipped), in order: for a template,
    one per chunk of text between its ${…} parts, whose code is read as code."""
    return parse(source).literals


def script_strings(source: str) -> list[str]:
    """The literals of a script that could be commands, unescaped: those passed to a command, and
    those not passed straight to a tool. A template is one string, its ${…} parts as written."""
    out, seen = [], set()
    for lit in literals(source):
        if lit.call is not None and not lit.shell:
            continue
        if lit.span in seen:
            continue
        seen.add(lit.span)
        out.append(_unescape(source[lit.span[0]:lit.span[1]]))
    return out


# Globals a script may use in a network tool's arguments that carry no data of their own: a value
# that does is a name of its own there (`JSON.stringify(token)` names `token`).
_HARMLESS = frozenset({"Date", "Math", "JSON", "String", "Number", "Boolean", "Object", "Array", "encodeURIComponent",
                       "decodeURIComponent", "encodeURI", "decodeURI", "parseInt", "parseFloat", "undefined", "NaN",
                       "Infinity", "console", "text"})


def computed_to_network(source: str, action_of) -> list[str]:
    """The network tools (`action_of(name)` is not "restore") a script hands a computed value to:
    a name other than a plain string constant, or another tool's result, when the
    script also runs a command. keepmum sees what goes to and from the model, so it never sees what
    such a value holds: a command's output can go to the server unchecked (`cat .env`, parsed, sent
    to an MCP tool). A script that reaches its tools by name (`tools[x]`) and runs a command is
    reported as "a tool the script names at run time"."""
    script = parse(source)
    if not script.calls & SHELL_CALLS:
        return []
    bound = set(script.bindings(source))
    out: list[str] = []
    for name, a, b in script.call_spans:
        decided = name
        if name in MCP_RESOURCE_CALLS:
            m = _SERVER_ARG.search(source, a, b)
            decided = f"mcp__{m.group(2) if m else '?'}__{name}"
        if name in SHELL_CALLS or action_of(decided) == "restore":
            continue
        # A name other than a plain constant, or another tool's result (a ${…} part is one of these).
        computed = any(a < r.start < b and r.name not in bound and r.name not in _HARMLESS for r in script.refs) \
            or any(a < x < y < b for _, x, y in script.call_spans)
        if computed and decided not in out:
            out.append(decided)
    if script.dynamic and not out:
        out.append("a tool the script names at run time")
    return out


def shown_tool(name: str) -> str:
    """A tool as a person reads it: `mcp__notes__send_note` -> "notes › send_note"."""
    if name.startswith("mcp__"):
        server, _, tool = name[5:].partition("__")
        return f"{server.removeprefix('claude_ai_')} › {tool}" if tool else server
    return name


class ScriptCheck:
    """Warns, never refuses, when a Codex code-mode script hands a computed value to a network tool
    (computed_to_network): keepmum cannot see that value, so a secret in a command's output can
    reach the server. `review(name, value, action_of)` gives the hits to record and the note to show
    in the agent, or ([], None)."""

    name = "script"
    label = "keepmum's script check"
    mode = "warn"

    def review(self, name: str, value, action_of) -> tuple[list[dict], str | None]:
        if name not in CODE_TOOLS or not isinstance(value, str):
            return [], None
        tools = computed_to_network(value, action_of)
        if not tools:
            return [], None
        shown = ", ".join(shown_tool(t) for t in tools)
        hit = {"check": self.name, "source": name, "score": 1.0, "chars": len(value), "earlier": False,
               "categories": [f"computed values sent to {shown_tool(t)}" for t in tools]}
        note = (f"keepmum: this script sends values it computes, such as a command's output, to {shown}. keepmum "
                "cannot see those values, so it cannot keep a secret out of them.")
        return [hit], note
