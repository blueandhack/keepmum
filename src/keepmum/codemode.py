"""Codex's code mode: the model's tool calls are a JavaScript program for the `exec` tool.

Since Codex 0.147 a model does not call `exec_command` or `apply_patch` itself. It writes a script
that does (`await tools.exec_command({cmd: "curl …"})`), and Codex runs it in a V8 isolate that has
no network of its own: the script reaches the network only through the nested tools. So the
commands a script can run are in its string literals. keepmum reads those, the way it reads a
Bash command, for the shell checks (destinations.py, commands.py).

A literal passed to `tools.apply_patch(…)` is a file's new text, not a command: it is left out, so a
URL in a file being written does not keep a token out of that file.
"""

from __future__ import annotations

import re

# Tools whose input is a code-mode script, when it is a string.
CODE_TOOLS = frozenset({"exec"})

_ESCAPE = re.compile(r"\\(u\{[0-9a-fA-F]+\}|u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|\r\n|[\s\S])")
_SIMPLE = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0", "\n": "", "\r\n": ""}
_PATCH_CALL = re.compile(r"\bapply_patch\s*\(|\[\s*[\"'`]apply_patch[\"'`]\s*\]\s*\(")


def _unescape(raw: str) -> str:
    def one(m):
        e = m.group(1)
        if e.startswith("u{"):
            return chr(int(e[2:-1], 16))
        if e[0] in "ux" and len(e) > 1:
            return chr(int(e[1:], 16))
        return _SIMPLE.get(e, e)

    return _ESCAPE.sub(one, raw)


def literals(source: str) -> list[tuple[int, int, str, bool]]:
    """The string and template literals of a script: (start, end) of each one's text between its
    quotes, the quote, and whether it is inside a call to apply_patch. Comments are skipped; a
    template literal's ${…} parts are read as part of its text."""
    out: list[tuple[int, int, str, bool]] = []
    i, n = 0, len(source)
    patch_depth = 0  # paren depth inside an apply_patch( call; 0 = outside
    while i < n:
        c = source[i]
        if c == "/" and source.startswith("//", i):
            j = source.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and source.startswith("/*", i):
            j = source.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "'\"`":
            j = i + 1
            while j < n and source[j] != c:
                if source[j] == "\\":
                    j += 2
                    continue
                if c != "`" and source[j] == "\n":  # an unterminated literal ends at the line
                    break
                j += 1
            out.append((i + 1, min(j, n), c, bool(patch_depth)))
            i = j + 1
            continue
        if patch_depth:
            if c == "(":
                patch_depth += 1
            elif c == ")":
                patch_depth -= 1
        elif (m := _PATCH_CALL.match(source, i)) and (i == 0 or not (source[i - 1].isalnum() or source[i - 1] == "_")):
            patch_depth = 1
            i = m.end()
            continue
        i += 1
    return out


def script_strings(source: str) -> list[str]:
    """The string and template literals of a script, unescaped, except those passed to apply_patch."""
    return [_unescape(source[a:b]) for a, b, _, in_patch in literals(source) if not in_patch]
