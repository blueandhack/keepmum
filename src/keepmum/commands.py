"""The commands check: shell commands a model asks the agent to run, held to a few rules.

Off by default (`keepmum guard commands warn|block`). It reads the tool calls in a reply, as they
come back, for the shell tools agents run commands with (destinations.SHELL_TOOLS), and looks for
what a planted instruction usually wants done. No model: rules, fast and predictable.

    warn   the command goes to the agent as it is; the hit is logged, audited and shown (Activity,
           the flow diagram's Flagged band)
    block  the agent gets, in its place, a command that only prints why keepmum refused it and
           exits with an error: the model reads that as the tool's result and can explain, or ask
           the user. An error in the stream instead would make agents retry the whole request.

A hit keeps the rule and the tool, never the command: it may hold real values. The rules are for
the clear cases; a command built to hide what it does gets past them, so the restore guards
(destinations.py, tools.py) still decide which real values a command may carry.
"""

from __future__ import annotations

import re
import shlex

from .destinations import SHELL_TOOLS

_SHELL = r"(?:sudo\s+(?:-\S+\s+)*)?(?:env\s+\S+=\S*\s+)*(?:/\S*/)?(?:ba|z|da|k|fi|c|tc)?sh\b"
_INTERPRETER = rf"(?:{_SHELL}|(?:/\S*/)?(?:python[0-9.]*|perl|ruby|node|php|osascript)\b)"
_FETCH = r"\b(?:curl|wget|fetch|aria2c|http|https|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b"
_NETWORK = (r"(?:\b(?:curl|wget|nc|ncat|netcat|socat|scp|sftp|ftp|telnet|http|https|aria2c|Invoke-WebRequest|"
            r"Invoke-RestMethod)\b|/dev/(?:tcp|udp)/|\brsync\b[^|;&\n]*\S+:\S*)")
_CREDENTIALS = (r"(?:\.ssh\b|\bid_(?:rsa|dsa|ecdsa|ed25519)\b|\.aws/credentials|\.netrc\b|\.git-credentials\b|"
                r"\.docker/config\.json|\.kube/config|\.config/gh/hosts\.yml|\.npmrc\b|\.pypirc\b|\.config/gcloud/|"
                r"Library/Keychains|\bsecurity\s+(?:find|dump)-(?:generic|internet)-password|\bsecurity\s+dump-keychain)")
_ENVIRONMENT = r"(?:^|[|;&(`]\s*|\$\()\s*(?:env|printenv|set|export\s+-p)\s*(?:[|;&)`]|$)"
_HOME = r"(?:~|\$HOME|\$\{HOME\}|\"\$HOME\"|\"\$\{HOME\}\"|/Users/[^/\s;|&]+|/home/[^/\s;|&]+)"
_ROOT_TARGET = rf"(?:/|/\*|{_HOME}/?|{_HOME}/\*|/Users/?|/home/?|/System\b\S*|/usr/?|/etc/?|/Library/?)"

# (id, what it means to the user, pattern over the whole command line)
RULES: tuple[tuple[str, str, re.Pattern], ...] = tuple((rid, label, re.compile(p, re.I | re.M)) for rid, label, p in (
    ("download-to-shell", "a download run as a script",
     rf"{_FETCH}[^|;&\n]*\|\s*{_INTERPRETER}|{_SHELL}\s+(?:-\S+\s+)*<\(\s*{_FETCH}|"
     rf"{_INTERPRETER}\s+(?:-\S+\s+)*-c\s+[\"']?\$\(\s*{_FETCH}|\b(?:source|\.)\s+<\(\s*{_FETCH}|"
     rf"\b(?:iex|Invoke-Expression)\b[^\n]*(?:iwr|irm|Invoke-WebRequest|Invoke-RestMethod|DownloadString)"),
    ("decode-and-run", "decoded text run as a script",
     rf"\bbase64\s+(?:-\S*[dD]\S*|--decode)\b[^|;&\n]*\|\s*{_INTERPRETER}|\beval\s+[\"']?\$\([^)]*\bbase64\s+(?:-\S*[dD]|--decode)|"
     rf"\bxxd\s+-r\b[^|;&\n]*\|\s*{_INTERPRETER}"),
    ("delete-home-or-system", "deleting your home folder or the system",
     rf"\brm\s+(?:-[a-zA-Z]*\s+|--[a-z-]+\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+(?:-\S+\s+)*{_ROOT_TARGET}(?=\s|$|;|&|\|)|"
     rf"\brm\s+(?:-[a-zA-Z]*\s+|--[a-z-]+\s+)*--recursive\s+(?:-\S+\s+)*{_ROOT_TARGET}(?=\s|$|;|&|\|)"),
    ("wipe-disk", "erasing a disk",
     r"\bmkfs(?:\.\w+)?\s|\bdd\s[^\n]*\bof=/dev/(?:r?disk\d|sd[a-z]|nvme\d|hd[a-z])|\bdiskutil\s+(?:erase\w*|zeroDisk|secureErase)\b"),
    ("fork-bomb", "a fork bomb", r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
))
# Two patterns that must both appear: secrets read and sent over the network in one command.
SENDS = (("credentials-sent", "credential files sent over the network", re.compile(_CREDENTIALS, re.I)),
         ("environment-sent", "environment variables sent over the network", re.compile(_ENVIRONMENT, re.I | re.M)))
_NETWORK_RE = re.compile(_NETWORK, re.I)
LABELS = {rid: label for rid, label, _ in RULES} | {rid: label for rid, label, _ in SENDS}


def command_lines(name: str, value) -> list[str]:
    """The command lines in a shell tool's input: strings, and argv lists joined (not descriptions)."""
    from .restore import _command_texts

    if name not in SHELL_TOOLS:
        return []
    return [t for t in _command_texts(value) if isinstance(t, str) and t.strip()]


# Text that echo or printf only prints is not run: single-quoted, or double-quoted without $( or `.
_PRINTED = re.compile(r"""(\b(?:echo|printf)\s+(?:-\S+\s+)*(?:''\s+)*)('[^']*'|"[^"$`]*")""")


def check(text: str) -> list[str]:
    """The rules a command line breaks, as ids."""
    while (printed := _PRINTED.sub(r"\1''", text)) != text:  # every argument in turn
        text = printed
    hits = [rid for rid, _, rx in RULES if rx.search(text)]
    if _NETWORK_RE.search(text):
        hits += [rid for rid, _, rx in SENDS if rx.search(text)]
    return hits


def refusal(name: str, value, labels: list[str]):
    """The tool input to send in place of a refused command: it prints why, and fails. The same
    shape as the model's (a command string, or an argv list), with its other fields kept."""
    message = (f"keepmum refused this command: {', '.join(labels)}. It was not run. If the user wants it run, "
               "they can run it themselves, or switch the commands check to warn (keepmum guard commands warn).")
    script = f"printf '%s\\n' {shlex.quote(message)} >&2; exit 1"

    def swap(v, key=None):
        if isinstance(v, dict):
            return {k: (swap(x, k) if k in ("command", "cmd", "commands", "args", "argv") else x) for k, x in v.items()}
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            return ["sh", "-c", script]
        if isinstance(v, str):
            return script
        return v

    return swap(value)


class CommandsCheck:
    """The check as the proxy runs it, on each tool call in a reply. `mode` is a callable (the live
    config). `review(name, value)` gives None (let it be) or the input to send in its place, and
    the hits it found, for the caller to record: [{check, source, score, chars, categories}]."""

    name = "commands"

    def __init__(self, mode):
        self._mode = mode if callable(mode) else (lambda: mode)

    @property
    def mode(self) -> str:
        m = self._mode()
        return m if m in ("off", "warn", "block") else "off"

    def review(self, name: str, value) -> tuple[object | None, list[dict]]:
        if self.mode == "off":
            return None, []
        lines = command_lines(name, value)
        found = sorted({rid for line in lines for rid in check(line)})
        if not found:
            return None, []
        labels = [LABELS[r] for r in found]
        hit = {"check": self.name, "source": name, "score": 1.0, "chars": sum(map(len, lines)), "earlier": False,
               "categories": labels}
        return (refusal(name, value, labels) if self.mode == "block" else None), [hit]
