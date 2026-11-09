"""Put a real value back in the syntax around its placeholder.

The model writes around a placeholder, `KEEPMUM_SECRET_1a2b3c4d`, which needs no quoting anywhere.
The real value may: `Xk9$mQ2…` in `echo "DB_PASSWORD=…"` would be expanded by the shell, and
`c0rrect\\horse` in a JavaScript string would lose its backslash. So a value going back into a tool
call is encoded for where its placeholder sits:

- a shell command: unquoted, in '…', in "…", in a heredoc (quoted delimiter: as is)
- a Codex code-mode script (codemode.py): in a JavaScript string literal; a literal passed to a
  command, or one that may become a command, is quoted for the shell first, then for JavaScript
- a JSON document in a tool's input (a file being written): in a JSON string

A value that needs no quoting (a token: letters, digits, `_-./:@+=,%`) is put back as it is, so
ordinary restores are byte for byte what they were.
"""

from __future__ import annotations

import json
import re

from .vault import PLACEHOLDER

_PLAIN = re.compile(r"[A-Za-z0-9_@%+=:,./-]*")
_HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|(\\?)([A-Za-z_][\w.-]*))")
_JSON_STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"')


# --- the shell ----------------------------------------------------------------------------

def shell_quote(value: str, ctx: str) -> str:
    """value as the shell reads it in context ctx: unquoted, single, double, heredoc, raw; or
    `outer>inner`, in a script a shell runs (`bash -lc '…'`): quoted for the inner script first,
    then for the quotes around the script."""
    if ">" in ctx:
        outer, inner = ctx.split(">", 1)
        return shell_quote(shell_quote(value, inner), outer)
    if ctx == "raw" or _PLAIN.fullmatch(value):
        return value
    if ctx == "single":
        return value.replace("'", "'\\''")
    if ctx == "double":
        return re.sub(r'([\\"$`])', r"\\\1", value)
    if ctx == "heredoc":
        return re.sub(r"([\\$`])", r"\\\1", value)
    return "'" + value.replace("'", "'\\''") + "'"


def escaped_forms(value: str) -> set[str]:
    """How value reads once keepmum has put it back escaped: for each shell context, each JavaScript
    quote (a shell-quoted literal in a code-mode script, too) and a JSON string. An agent sends the
    commands it ran back in its history: escaped, the value is none of the text keepmum masked,
    so it went to the provider as it read (or masked anew, under a placeholder the model never
    wrote). The vault maps each form to the value's own placeholder (vault.Vault.placeholder)."""
    shell = {shell_quote(value, ctx) for ctx in ("single", "double", "heredoc", "unquoted")}
    shell |= {shell_quote(value, f"{outer}>{inner}") for outer in ("single", "double")
              for inner in ("single", "double", "unquoted")}
    script = {js_escape(v, q) for v in shell | {value} for q in "\"'`"}
    return (shell | script | {json.dumps(value, ensure_ascii=False)[1:-1]}) - {value}


# A shell given a script to run: `bash -lc '…'`, `sh -c "…"`, `/bin/zsh -euc …`.
_SHELL_C = re.compile(r"(?:^|(?<=[\s;&|(`]))(?:[\w./-]*/)?(?:bash|sh|zsh|dash|ksh)\s+(?:-[A-Za-z]+\s+(?:pipefail\s+)?)*?"
                      r"-[A-Za-z]*c[A-Za-z]*\s+")


def _script_word(cmd: str, start: int) -> tuple[int, str, list[int]]:
    """The shell word at `start`: where it ends, the text it stands for (the script a shell will
    run) and, for each of that text's characters, where it sits in `cmd`."""
    text, at = [], []
    i, n = start, len(cmd)
    while i < n:
        c = cmd[i]
        if c == "'":
            j = cmd.find("'", i + 1)
            j = n if j < 0 else j
            text += cmd[i + 1:j]
            at += range(i + 1, j)
            i = j + 1
        elif c == '"':
            i += 1
            while i < n and cmd[i] != '"':
                if cmd[i] == "\\" and i + 1 < n and cmd[i + 1] in '\\"$`':
                    i += 1
                text.append(cmd[i])
                at.append(i)
                i += 1
            i += 1
        elif c == "\\" and i + 1 < n:
            text.append(cmd[i + 1])
            at.append(i + 1)
            i += 2
        elif c.isspace() or c in ";&|)":
            break
        else:
            text.append(c)
            at.append(i)
            i += 1
    return i, "".join(text), at


def shell_contexts(cmd: str, positions: list[int]) -> dict[int, str]:
    """The quoting context at each position of a shell command: unquoted, single, double,
    heredoc (an unquoted delimiter: $ and ` still expand) or raw (a quoted delimiter: as is).
    A $( … ) starts a new unquoted context, inside double quotes too. In a script a shell is
    given to run (`bash -lc '…'`), `outer>inner`: the quotes around the script, then the context in
    it, so a value is quoted for both (a name with a space, split in two by the inner shell, went
    out in pieces in its error messages: a live Codex run on gpt-5-nano, fn-04)."""
    out = _shell_contexts(cmd, positions)
    for m in _SHELL_C.finditer(cmd):
        end, script, at = _script_word(cmd, m.end())
        inside = {at.index(p): p for p in positions if m.end() <= p < end and p in at}
        if not inside:
            continue
        for q, ctx in shell_contexts(script, list(inside)).items():
            p = inside[q]
            if out[p] in ("single", "double", "unquoted"):
                out[p] = f"{out[p]}>{ctx}"
    return out


def _shell_contexts(cmd: str, positions: list[int]) -> dict[int, str]:
    want = set(positions)
    out: dict[int, str] = {}
    ctx = ["unquoted"]
    pending: list[tuple[str, bool, bool]] = []  # heredocs opened on this line: (delimiter, quoted, strip tabs)
    body: list[tuple[str, bool, bool]] = []  # the heredoc being read
    i, n = 0, len(cmd)
    while i < n:
        top = ctx[-1]
        if top in ("heredoc", "raw"):  # at the start of a line of a heredoc's body
            end = cmd.find("\n", i)
            end = n if end < 0 else end
            for p in want:
                if i <= p < end:
                    out[p] = top
            delim, _, strip = body[-1]
            if (cmd[i:end].lstrip("\t") if strip else cmd[i:end]) == delim:
                ctx.pop()
                body.pop()
                if pending:
                    body.append(pending.pop(0))
                    ctx.append("raw" if body[-1][1] else "heredoc")
            i = end + 1
            continue
        if i in want:
            out[i] = top
        c = cmd[i]
        if top == "single":
            if c == "'":
                ctx.pop()
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if cmd.startswith("$(", i):
            ctx.append("unquoted")
            i += 2
            continue
        if top == "double":
            if c == '"':
                ctx.pop()
            i += 1
            continue
        if c == "'":
            ctx.append("single")
        elif c == '"':
            ctx.append("double")
        elif c == ")" and len(ctx) > 1:
            ctx.pop()
        elif c == "<" and (m := _HEREDOC.match(cmd, i)):
            delim = m.group(2) or m.group(3) or m.group(5)
            pending.append((delim, bool(m.group(2) or m.group(3) or m.group(4)), bool(m.group(1))))
            i = m.end()
            continue
        elif c == "\n" and pending:
            body.append(pending.pop(0))
            ctx.append("raw" if body[-1][1] else "heredoc")
        i += 1
    for p in want:
        out.setdefault(p, ctx[-1])
    return out


def restore_command(cmd: str, swap) -> str:
    """A shell command with each placeholder put back, quoted for where it sits. `swap(match)`
    gives the value, or None to leave the placeholder."""
    matches = list(PLACEHOLDER.finditer(cmd))
    if not matches:
        return cmd
    ctx = shell_contexts(cmd, [m.start() for m in matches])
    out, pos = [], 0
    for m in matches:
        value = swap(m)
        out.append(cmd[pos:m.start()])
        out.append(m.group(0) if value is None else shell_quote(value, ctx[m.start()]))
        pos = m.end()
    out.append(cmd[pos:])
    return "".join(out)


# --- JavaScript (Codex code mode) ----------------------------------------------------------

def js_escape(value: str, quote: str) -> str:
    out = value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\u2028", "\\u2028") \
        .replace("\u2029", "\\u2029")
    if quote == "`":
        return out.replace("`", "\\`").replace("${", "\\${")
    return out.replace(quote, "\\" + quote)


def restore_script(source: str, swap_for, edits: list[tuple[int, int, str]] = ()) -> str:
    """A code-mode script with each placeholder put back, escaped for its string literal, and
    quoted for the shell first when the literal may be a command. `swap_for(literal)` gives the
    swap for a codemode.Literal, or for None: code outside the literals; and whether the literal
    is shell text. `edits`: (start, end, text) to put in place of code outside the literals."""
    from .codemode import _unescape, literals

    if not PLACEHOLDER.search(source):
        return source
    out, pos = [], 0
    outside, _ = swap_for(None)
    edits = sorted(edits)

    def code(a: int, b: int) -> str:
        parts = []
        for start, end, text in edits:
            if a <= start < b:
                parts += [_restore_raw(source[a:start], outside), text]
                a = end
        return "".join(parts) + _restore_raw(source[a:b], outside)

    for lit in literals(source):
        raw = source[lit.start:lit.end]
        if not PLACEHOLDER.search(raw):
            continue
        out.append(code(pos, lit.start))
        swap, shell = swap_for(lit)
        pieces, last = [], 0
        matches = list(PLACEHOLDER.finditer(raw))
        # Positions in the whole literal: a template's chunks are one command.
        at = {m.start(): len(_unescape(source[lit.span[0]:lit.start + m.start()])) for m in matches} if shell else {}
        ctx = shell_contexts(_unescape(source[lit.span[0]:lit.span[1]]), list(at.values())) if shell else {}
        for m in matches:
            value = swap(m)
            pieces.append(raw[last:m.start()])
            if value is None:
                pieces.append(m.group(0))
            else:
                if shell:
                    value = shell_quote(value, ctx[at[m.start()]])
                pieces.append(js_escape(value, lit.quote))
            last = m.end()
        pieces.append(raw[last:])
        out.append("".join(pieces))
        pos = lit.end
    out.append(code(pos, len(source)))
    return "".join(out)


def _restore_raw(text: str, swap) -> str:
    return PLACEHOLDER.sub(lambda m: v if (v := swap(m)) is not None else m.group(0), text)


# --- JSON documents ---------------------------------------------------------------------------

def json_strings(text: str) -> list[tuple[int, int]] | None:
    """The insides of the string tokens of text, when text is one JSON object or array."""
    head = text.lstrip()[:1]
    if head not in ("{", "["):
        return None
    try:
        json.loads(text)
    except ValueError:
        return None
    return [(m.start() + 1, m.end() - 1) for m in _JSON_STRING.finditer(text)]


def restore_json_text(text: str, swap) -> str | None:
    """A JSON document with each placeholder in a string put back JSON-escaped; None when text is
    not one."""
    strings = json_strings(text)
    if strings is None:
        return None

    def inside(i):
        return any(a <= i < b for a, b in strings)

    def one(m):
        value = swap(m)
        if value is None:
            return m.group(0)
        return json.dumps(value, ensure_ascii=False)[1:-1] if inside(m.start()) else value

    return PLACEHOLDER.sub(one, text)
