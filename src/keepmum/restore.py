"""Put real values back where placeholders appear: whole responses and SSE streams."""

from __future__ import annotations

import json
import logging
import re
import shlex
from collections import Counter

from .detect import TYPES
from .vault import PLACEHOLDER, Vault, canonical

log = logging.getLogger("keepmum")

# Per tool: "restore" puts real values into the tool's input before it runs locally; "keep"
# leaves placeholders, so a prompt-injected request cannot send a real value over the network;
# "own" lets an MCP server have back the values it returned itself, and a list of types adds
# those (tools.py has the rules every network tool is held to).
DEFAULT_POLICY = {
    "default": "restore",
    "WebFetch": "keep",
    "WebSearch": "keep",
    "webfetch": "keep",
    "websearch": "keep",
    "web_fetch": "keep",
    "web_search": "keep",
    "codesearch": "keep",
    "mcp__*": "own",
    "mcp_*": "own",
}
ACTIONS = ("restore", "keep", "own")


class ToolPolicy(dict):
    """The restore policy, with the registry of servers seen (tools.ToolRegistry) when there is one,
    the hosts config [destinations] adds for issuer rules (destinations.py), and, per request, the
    commands check (commands.py): `review(name, value)` gives None, or the input to send instead."""

    def __init__(self, policy: dict, registry=None, destinations: dict[str, list[str]] | None = None, review=None):
        super().__init__(policy)
        self.registry = registry
        self.destinations = destinations or {}
        self.review = review

    def with_review(self, review) -> ToolPolicy:
        return ToolPolicy(self, self.registry, self.destinations, review)


def rule_for(name: str, policy: dict) -> tuple[str, object]:
    """(the key that decides, its action) for a tool: its own name, else the longest matching
    prefix pattern ("mcp__gmail__*" before "mcp__*"), else the default."""
    if name in policy:
        return name, policy[name]
    best = None
    for pattern in policy:
        if pattern.endswith("*") and name.startswith(pattern[:-1]) and (best is None or len(pattern) > len(best)):
            best = pattern
    if best is not None:
        return best, policy[best]
    return "default", policy.get("default", "restore")


def take_tools(counts: Counter) -> list[dict]:
    """Remove the tool decisions put in counts: [{tool, action, values, reason, types}], one per
    tool, action and reason. action is "restore" (real values went in) or "keep"; reason says why:
    local (the tool runs on this Mac), own (the server returned it), allowed (its type is let
    through), and for kept ones placeholders (the setting), secret, paused (the server changed),
    not_allowed, too_many."""
    keys = [k for k in counts if isinstance(k, tuple) and k[0] == TOOL]
    grouped: dict[tuple, dict] = {}
    for k in sorted(keys, key=lambda k: tuple(str(x) for x in k)):
        _, name, action, reason, kind = k
        n = counts.pop(k)
        g = grouped.setdefault((name, action, reason), {"tool": name, "action": action, "values": 0})
        g["values"] += n
        if reason:
            g["reason"] = reason
        if kind:
            g.setdefault("types", {})[kind] = g.get("types", {}).get(kind, 0) + n
    return list(grouped.values())


TOOL = "tool"

# Key in the counts Counter for placeholders the vault does not know: the model reused one this
# process never made (another keepmum with another salt, or one restarted since). They stay as
# they are, and the proxy reports them.
UNRESOLVED = "unresolved"


def resolve(m, vault: Vault) -> tuple[str, str | None, str]:
    """(the placeholder the vault knows, its value or None, text after it) for a PLACEHOLDER match.
    A 12-digit match may be an 8-digit placeholder followed by hex text."""
    ph = m.group(0)
    value = vault.value(ph)
    if value is None and len(m.group(2)) == 12 and vault.value(ph[:-4]) is not None:
        return ph[:-4], vault.value(ph[:-4]), ph[-4:]
    return ph, value, ""


def _in_json_string(text: str):
    """A test for positions inside a string of text, when text is one JSON document: a value put
    back there is JSON-escaped, as it was before it was masked (redact.py keeps values unescaped)."""
    from .encode import json_strings

    strings = json_strings(text) if text.lstrip()[:1] in ("{", "[") and PLACEHOLDER.search(text) else None
    if not strings:
        return lambda i: False
    return lambda i: any(a <= i < b for a, b in strings)


def _as_json(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)[1:-1]


def restore_text(text: str, vault: Vault, counts: Counter | None = None, only: set[str] | None = None) -> str:
    """Replace every placeholder the vault knows (with `only`, just those placeholders).
    Unknown placeholders stay as they are. In a JSON document, values in strings are escaped.
    A placeholder the model respelled (lower case, hyphens) is put back too."""
    text = canonical(text, vault)
    inside = _in_json_string(text)

    def swap(m):
        ph, value, suffix = resolve(m, vault)
        if value is None:
            if counts is not None:
                counts[UNRESOLVED] += 1
            return m.group(0)
        if only is not None and ph not in only:
            return m.group(0)
        if counts is not None:
            counts[ph.split("_")[1]] += 1
        return (_as_json(value) if inside(m.start()) else value) + suffix

    return PLACEHOLDER.sub(swap, text)


# Reply text a client shows: a placeholder inside a web address stays a placeholder. A client that
# renders the reply (an image, a link, a link preview) would otherwise send the real value to that
# address, wherever a prompt injection pointed it. Other schemes (postgres://, redis://) are
# restored: no renderer fetches them. The run of text since the last whitespace decides.
_WEB = re.compile(r"(?i)https?://|(?:^|[(\[<\"'=])//[^/\s]")
_RUN_CAP = 4096
URL = "url"  # counts key: (URL, type) for each placeholder kept in a web address


def _run(text: str) -> str:
    """The text after the last whitespace: the word, or address, `text` ends in."""
    i = len(text)
    while i and not text[i - 1].isspace():
        i -= 1
    return text[i:]


def next_run(before: str, text: str) -> str:
    """The run a reply ends in once `text` follows `before` (both as the model wrote them)."""
    run = _run(text)
    if len(run) == len(text):
        run = before + run
    if len(run) > _RUN_CAP:  # an address this long stays one: keep its scheme in view
        run = ("http://" if _WEB.search(run) else "") + run[-_RUN_CAP:]
    return run


def restore_reply(text: str, vault: Vault, counts: Counter | None = None, before: str = "") -> str:
    """restore_text for reply text a client shows, except placeholders inside an http(s) address.
    `before`: the run the reply ended in before `text` (next_run), when it came in earlier pieces."""
    text = canonical(text, vault)
    inside = _in_json_string(text)

    def swap(m):
        ph, value, suffix = resolve(m, vault)
        if value is not None and _WEB.search(next_run(before, text[: m.start()])):
            if counts is not None:
                counts[(URL, ph.split("_")[1])] += 1
            return m.group(0)
        restored = restore_text(m.group(0), vault, counts)
        if value is not None and inside(m.start()) and restored != m.group(0):
            return _as_json(value) + suffix
        return restored

    return PLACEHOLDER.sub(swap, text)


def take_urls(counts: Counter) -> dict[str, int]:
    """Remove the placeholders kept in web addresses from counts: {type: how many}."""
    keys = [k for k in counts if isinstance(k, tuple) and k[0] == URL]
    return {k[1]: counts.pop(k) for k in sorted(keys)}


def _values(value, vault: Vault, counts: Counter | None, only: set[str] | None = None):
    if isinstance(value, str):
        return restore_text(value, vault, counts, only)
    if isinstance(value, dict):
        return {(restore_text(k, vault, counts, only) if isinstance(k, str) else k): _values(v, vault, counts, only)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_values(v, vault, counts, only) for v in value]
    return value


def tool_values(name: str, value, vault: Vault, policy: dict) -> tuple[set[str] | None, list[tuple[str, str, str, int]]]:
    """Which placeholders in a tool call's input get their real values: (None for all, or the set
    allowed; and the decisions as (action, reason, type, occurrences)) for the audit log."""
    action = rule_for(name, policy)[1]
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    found = Counter()
    for m in PLACEHOLDER.finditer(text):
        ph, real, _ = resolve(m, vault)
        if real is not None:
            found[ph] += 1
    if action == "restore":
        wrong = _wrong_destinations(name, value, found, vault, policy)
        if not wrong:
            return None, [("restore", "local", ph.split("_")[1], n) for ph, n in found.items()]
        return set(found) - set(wrong), [("keep" if ph in wrong else "restore", "destination" if ph in wrong else "local",
                                          ph.split("_")[1], n) for ph, n in found.items()]
    choices = _network_choices(name, action, found, vault, policy)
    allowed = {ph for ph, (a, _) in choices.items() if a == "restore"}
    return allowed, [(a, r, ph.split("_")[1], found[ph]) for ph, (a, r) in choices.items()]


def _network_choices(name: str, action, found, vault: Vault, policy: dict) -> dict[str, tuple[str, str]]:
    """For a tool that is not "restore": each placeholder's (action, reason)."""
    from .tools import BULK, server_of

    server = server_of(name)
    registry = getattr(policy, "registry", None)
    types = set(action) if isinstance(action, (list, tuple)) else set()
    paused = action != "keep" and registry is not None and registry.paused(server)
    out = {}
    for ph in found:
        kind = ph.split("_")[1]
        if action == "keep":
            reason = "placeholders"
        elif kind == "SECRET":
            reason = "secret"
        elif paused:
            reason = "paused"
        elif server in vault.origins_of(vault.value(ph)):
            reason = "own"
        elif kind in types:
            reason = "allowed"
        else:
            reason = "not_allowed"
        out[ph] = ("restore" if reason in ("own", "allowed") else "keep", reason)
    if sum(a == "restore" for a, _ in out.values()) > BULK:
        out = {ph: ("keep", "too_many" if a == "restore" else r) for ph, (a, r) in out.items()}
    if registry is not None:
        registry.note_kept(server, [ph.split("_")[1] for ph, (_, r) in out.items() if r == "not_allowed"])
    return out


def _command_texts(value, name: str | None = None):
    """The strings of a shell tool's input that could be commands (not its description). An argv
    list is also read as one command line, so `["curl", "host"]` is still curl and its host. A
    code-mode script (codemode.py) gives its string literals, not the patches it applies."""
    from .codemode import CODE_TOOLS, script_strings

    if name in CODE_TOOLS and isinstance(value, str):
        yield from script_strings(value)
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        if value and all(isinstance(x, str) for x in value):
            yield shlex.join(value)
        for x in value:
            yield from _command_texts(x)
    elif isinstance(value, dict):
        for k, v in value.items():
            if k != "description":
                yield from _command_texts(v)


def _wrong_destinations(name: str, value, found: Counter, vault: Vault, policy: dict) -> set[str]:
    """The placeholders a shell command would send to a host their issuer does not use (destinations.py)."""
    from .destinations import ISSUERS, SHELL_TOOLS, allowed_hosts, hosts_in, shown, wrong_hosts

    if name not in SHELL_TOOLS or not found:
        return set()
    texts = list(_command_texts(value, name))
    hosts = set().union(*(hosts_in(t) for t in texts))
    if not hosts:
        return set()
    # Only a placeholder in what runs can be sent: one in a description, or in a patch a code-mode
    # script applies, is not.
    commands = "\n".join(texts)
    wrong = set()
    for ph in (ph for ph in found if ph in commands):
        sources = vault.sources_of(vault.value(ph))
        allowed = allowed_hosts(sources, getattr(policy, "destinations", None))
        bad = wrong_hosts(hosts, allowed) if allowed is not None else []
        if bad:
            wrong.add(ph)
            extra = getattr(policy, "destinations", None) or {}
            rules = sorted(r for r in (s.removeprefix("rules:") for s in sources) if r in ISSUERS or r in extra)
            log.warning("%s: kept a placeholder for a value found by %s: the command sends it to %s, a host its "
                        "issuer does not use (`keepmum destinations add` adds hosts)", name, ", ".join(rules),
                        ", ".join(shown(h) for h in bad))
    return wrong


def restore_tool(name: str, value, vault: Vault, policy: dict, counts: Counter | None, parse: bool = False):
    """A tool call's input with the real values its tool may receive, and its decisions in counts.
    `parse`: value is a JSON document in a string (Responses API arguments), restored as JSON."""
    tree = value
    if parse and isinstance(value, str):
        try:
            tree = json.loads(value)
        except ValueError:
            parse = False
    tree = _canonical_tree(tree, vault)
    if not parse:
        value = tree
    if (typo := _typo(name, tree, vault)) is not None:
        from .commands import refusal

        if counts is not None:
            counts[UNRESOLVED] += 1
        refused = refusal(name, tree, [], message=(
            f"keepmum could not run this: {typo[0]} is not a placeholder from this conversation (did you mean "
            f"{typo[1]}?). It was not run. Copy each KEEPMUM_ placeholder exactly as it appears earlier."))
        return json.dumps(refused, ensure_ascii=False) if parse else refused
    review = getattr(policy, "review", None)
    if review is not None and (replaced := review(name, tree)) is not None:
        return json.dumps(replaced, ensure_ascii=False) if parse else replaced  # a refused command: no values go in
    from .codemode import CODE_TOOLS

    if name in CODE_TOOLS and isinstance(tree, str):
        return _restore_script_tool(name, tree, vault, policy, counts)
    only, decisions = tool_values(name, tree, vault, policy)
    if counts is not None:
        for action, reason, kind, n in decisions:
            counts[(TOOL, name, action, reason, kind)] += n
    if only is not None and not only:
        return value  # nothing to put back: the input goes out exactly as the model wrote it
    if parse:
        return json.dumps(_tool_values(name, tree, vault, counts, only), ensure_ascii=False)
    return _tool_values(name, value, vault, counts, only)


def _found(text: str, vault: Vault) -> Counter:
    """The placeholders in text that the vault knows, with how often each appears."""
    found = Counter()
    for m in PLACEHOLDER.finditer(text):
        ph, real, _ = resolve(m, vault)
        if real is not None:
            found[ph] += 1
    return found


def _restore_script_tool(name: str, source: str, vault: Vault, policy: dict, counts: Counter | None) -> str:
    """A code-mode script with real values put back literal by literal, each held to the nested
    tool it is passed to (codemode.py): a command's and a patch's get them, as `exec` would; an MCP
    server's only what that server may receive. A literal not passed straight to a tool, and code
    outside the literals, get only what every network tool the script names may receive, and
    nothing when the script can reach a tool it does not name."""
    from .codemode import SHELL_CALLS, _unescape, parse
    from .encode import restore_script, shell_contexts, shell_quote

    script = parse(source)
    network = script.network(source, lambda n: rule_for(n, policy)[1])
    wrong = _wrong_destinations(name, source, _found(source, vault), vault, policy)

    # One decision per target: a call (its literals together), or "free" (everything else).
    def target(lit):
        if lit is None or lit.call is None:
            return None
        return script.tool_name(lit, source), lit.call_span

    texts: dict = {}
    pos = 0
    for lit in script.literals:
        texts.setdefault(None, []).append(source[pos:lit.start - 1])
        texts.setdefault(target(lit), []).append(_unescape(source[lit.start:lit.end]))
        pos = lit.end + 1
    texts.setdefault(None, []).append(source[pos:])

    choices: dict = {}  # target -> {ph: (tool, action, reason)}
    for key, parts in texts.items():
        found = _found("\n".join(parts), vault)
        if not found:
            continue
        if key is not None and rule_for(key[0], policy)[1] != "restore":
            got = _network_choices(key[0], rule_for(key[0], policy)[1], found, vault, policy)
            choice = {ph: (key[0], a, r) for ph, (a, r) in got.items()}
        elif key is None and script.dynamic:
            choice = {ph: (name, "keep", "script") for ph in found}
        elif key is None and network:
            choice = {ph: (name, "restore", "local") for ph in found}
            for tool in sorted(network):
                got = _network_choices(tool, rule_for(tool, policy)[1], found, vault, policy)
                for ph, (a, r) in got.items():
                    if choice[ph][1] == "restore":
                        choice[ph] = (tool, a, r)
        else:
            choice = {ph: (name, "restore", "local") for ph in found}
        # A script caught sending a token to a host its issuer does not use gets it nowhere.
        for ph in found:
            if ph in wrong and choice[ph][1] == "restore":
                choice[ph] = (name, "keep", "destination")
        choices[key] = choice
        if counts is not None:
            for ph, n in found.items():
                tool, a, r = choice[ph]
                counts[(TOOL, tool, a, r, ph.split("_")[1])] += n

    # A name bound once to a literal (`const tok = "…"`) and passed to a command where the literal
    # itself keeps a placeholder (the script also sends it to a network tool): that use gets the
    # literal with the real values the command may receive, written in its place.
    edits = []
    bound = script.bindings(source) if network and not script.dynamic else {}
    free = choices.get(None, {})
    for ref in script.refs:
        lit = bound.get(ref.name)
        if lit is None or ref.call is None or rule_for(script.tool_name(ref, source), policy)[1] != "restore":
            continue
        raw = source[lit.start:lit.end]
        if not any(free.get(resolve(m, vault)[0], ("", "keep"))[1] != "restore" for m in PLACEHOLDER.finditer(raw)):
            continue
        text = _unescape(raw)
        matches = list(PLACEHOLDER.finditer(text))
        ctx = shell_contexts(text, [m.start() for m in matches]) if ref.call in SHELL_CALLS else {}
        pieces, last = [], 0
        for m in matches:
            ph, real, suffix = resolve(m, vault)
            pieces.append(text[last:m.start()])
            if real is None or ph in wrong:
                pieces.append(m.group(0))
            else:
                pieces.append((shell_quote(real, ctx[m.start()]) if ctx else real) + suffix)
                if counts is not None:
                    counts[(TOOL, name, "restore", "local", ph.split("_")[1])] += 1
                    counts[ph.split("_")[1]] += 1
            last = m.end()
        pieces.append(text[last:])
        value = json.dumps("".join(pieces), ensure_ascii=False)
        edits.append((ref.start, ref.end, f"{ref.name}: {value}" if ref.shorthand else value))

    def swap_for(lit):
        choice = choices.get(target(lit), {})

        def swap(m):
            ph, real, suffix = resolve(m, vault)
            if real is None:
                if counts is not None:
                    counts[UNRESOLVED] += 1
                return None
            if choice.get(ph, ("", "keep"))[1] != "restore":
                return None
            if counts is not None:
                counts[ph.split("_")[1]] += 1
            return real + suffix

        # A free literal may be a command when the script runs one.
        return swap, lit is not None and (lit.shell or (lit.call is None and commands))

    commands = script.dynamic or bool(script.calls & SHELL_CALLS)
    return restore_script(source, swap_for, edits)


def _canonical_tree(value, vault: Vault):
    """Every string in a tool's input (keys too) with respelled placeholders written exactly."""
    if isinstance(value, str):
        return canonical(value, vault)
    if isinstance(value, dict):
        return {(canonical(k, vault) if isinstance(k, str) else k): _canonical_tree(v, vault) for k, v in value.items()}
    if isinstance(value, list):
        return [_canonical_tree(v, vault) for v in value]
    return value


def _typo(name: str, value, vault: Vault) -> tuple[str, str] | None:
    """(the placeholder, the one it is a typo of) when a command holds a placeholder keepmum did not
    make but nearly did: run, it would name a file or write a value `KEEPMUM_…`. Only commands: a
    file being written can hold placeholder-shaped text on purpose (keepmum's own tests)."""
    from .codemode import CODE_TOOLS
    from .destinations import SHELL_TOOLS

    if name not in SHELL_TOOLS and name not in CODE_TOOLS:
        return None
    for text in _command_texts(value, name):
        for m in PLACEHOLDER.finditer(text):
            if resolve(m, vault)[1] is None and (near := vault.near(m.group(0))) is not None:
                return m.group(0), near
    return None


_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})
_SHELL_FLAG = re.compile(r"-[a-z]*c[a-z]*")


def _tool_values(name: str, value, vault: Vault, counts: Counter | None, only: set[str] | None):
    """A tool's input with its real values, each encoded for where its placeholder sits (encode.py):
    quoted for the shell in a command, escaped in a code-mode script's literals or in a JSON
    document being written, and as it is everywhere else."""
    from .destinations import SHELL_TOOLS
    from .encode import restore_command, restore_json_text

    def swap(m):
        ph, real, suffix = resolve(m, vault)
        if real is None:
            if counts is not None:
                counts[UNRESOLVED] += 1
            return None
        if only is not None and ph not in only:
            return None
        if counts is not None:
            counts[ph.split("_")[1]] += 1
        return real + suffix

    def plain(text: str) -> str:
        return PLACEHOLDER.sub(lambda m: v if (v := swap(m)) is not None else m.group(0), text)

    shell = name in SHELL_TOOLS

    def walk(v, key=None):
        if isinstance(v, str):
            if shell and key in ("command", "cmd", "commands", "script"):
                return restore_command(v, swap)
            doc = restore_json_text(v, swap) if PLACEHOLDER.search(v) else None
            return doc if doc is not None else plain(v)
        if isinstance(v, dict):
            return {(plain(k) if isinstance(k, str) else k): walk(x, k) for k, x in v.items()}
        if isinstance(v, list):
            if shell and v and all(isinstance(x, str) for x in v) and key != "commands":
                # argv: only the script after `bash -c` is shell; other arguments go as they are
                program = v[0].rsplit("/", 1)[-1]
                return [restore_command(x, swap) if i > 0 and program in _SHELLS and _SHELL_FLAG.fullmatch(v[i - 1])
                        else plain(x) for i, x in enumerate(v)]
            return [walk(x, key) for x in v]
        return v

    return walk(value)


def restore_response(body: dict, vault: Vault, policy: dict[str, str] = DEFAULT_POLICY, counts: Counter | None = None) -> dict:
    """A copy of a non-streaming /v1/messages response with placeholders restored.

    Text is restored. Tool inputs follow the per-tool policy. Thinking blocks are signed and
    must be resent unchanged, so they keep their placeholders.
    """
    content = body.get("content")
    if not isinstance(content, list):
        return body
    out = []
    for block in content:
        if not isinstance(block, dict):
            out.append(block)
        elif block.get("type") == "text":
            out.append({**block, "text": restore_reply(block.get("text", ""), vault, counts)})
        elif block.get("type") == "tool_use":
            out.append({**block, "input": restore_tool(block.get("name", ""), block.get("input"), vault, policy, counts)})
        else:
            out.append(block)
    return {**body, "content": out}


# --- Streaming (server-sent events) -------------------------------------------------

_PREFIX = "KEEPMUM_"
_TYPE_TAILS = tuple(f"{t}_" for t in TYPES)
_HEX = frozenset("0123456789abcdef")


def _could_be_placeholder_start(s: str) -> bool:
    """True when more text could still turn s (a suffix of the stream) into a placeholder."""
    if len(s) <= len(_PREFIX):
        return _PREFIX.startswith(s)
    if not s.startswith(_PREFIX):
        return False
    rest = s[len(_PREFIX):]
    for tail in _TYPE_TAILS:
        if len(rest) <= len(tail):
            if tail.startswith(rest):
                return True
        elif rest.startswith(tail):
            digits = rest[len(tail):]
            # 8 digits is complete, but 4 more would make it a 12-digit placeholder.
            return len(digits) < 12 and all(c in _HEX for c in digits)
    return False


def _hold_from(text: str) -> int:
    """Index from which text must be held back, or len(text) when it can all go out now."""
    longest = len(_PREFIX) + max(len(t) for t in _TYPE_TAILS) + 12
    for i in range(max(0, len(text) - longest), len(text)):
        if text[i] == _PREFIX[0] and _could_be_placeholder_start(text[i:]):
            return i
    return len(text)


class StreamRestorer:
    """Restores placeholders in a /v1/messages SSE stream, chunk by chunk.

    - text_delta: a tail buffer holds back anything that could still become a placeholder.
    - input_json_delta (tool_use): the whole input is held until content_block_stop, then
      parsed, restored per the tool policy, and sent as one delta, so values are JSON-escaped.
    - Everything else (thinking and signatures, server tools, ping, message_*, error) passes
      through byte for byte, in order.
    """

    def __init__(self, vault: Vault, policy: dict[str, str] = DEFAULT_POLICY, counts: Counter | None = None):
        self.vault = vault
        self.policy = policy
        self.counts = counts if counts is not None else Counter()
        self._buf = b""
        self._blocks: dict[int, dict] = {}  # index -> {"type", "name", "pending", "json"}

    def feed(self, chunk: bytes) -> bytes:
        self._buf += chunk
        out = []
        while True:
            end, sep = self._event_end()
            if end < 0:
                break
            raw, self._buf = self._buf[: end + sep], self._buf[end + sep :]
            out.append(self._event(raw))
        return b"".join(out)

    def close(self) -> bytes:
        """Whatever is left when the upstream closes: an unterminated event goes out as it came."""
        rest, self._buf = self._buf, b""
        return rest

    def _event_end(self) -> tuple[int, int]:
        best = (-1, 0)
        for sep in (b"\n\n", b"\r\n\r\n"):
            i = self._buf.find(sep)
            if i >= 0 and (best[0] < 0 or i < best[0]):
                best = (i, len(sep))
        return best

    def _event(self, raw: bytes) -> bytes:
        try:
            text = raw.decode("utf-8")
            name, data_lines = None, []
            for line in text.splitlines():
                if line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip(" "))
            if not data_lines:
                return raw
            data = json.loads("\n".join(data_lines))
        except (UnicodeDecodeError, ValueError):
            return raw  # not ours to interpret: placeholders stay, nothing leaks
        kind = data.get("type")
        index = data.get("index")
        if kind == "content_block_start":
            block = data.get("content_block") or {}
            self._blocks[index] = {"type": block.get("type"), "name": block.get("name", ""), "pending": "", "json": [], "run": ""}
            return raw
        state = self._blocks.get(index)
        if kind == "content_block_delta" and state:
            delta = data.get("delta") or {}
            if delta.get("type") == "text_delta" and state["type"] == "text":
                text = state["pending"] + delta.get("text", "")
                cut = _hold_from(text)
                state["pending"] = text[cut:]
                ready = restore_reply(text[:cut], self.vault, self.counts, state["run"])
                state["run"] = next_run(state["run"], text[:cut])
                return self._emit(name, {**data, "delta": {**delta, "text": ready}}) if ready else b""
            if delta.get("type") == "input_json_delta" and state["type"] == "tool_use":
                state["json"].append(delta.get("partial_json", ""))
                return b""
            return raw
        if kind == "content_block_stop" and state:
            del self._blocks[index]
            return self._flush(index, state) + raw
        return raw

    def _flush(self, index: int, state: dict) -> bytes:
        if state["type"] == "text" and state["pending"]:
            text = restore_reply(state["pending"], self.vault, self.counts, state["run"])
            delta = {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}}
            return self._emit("content_block_delta", delta)
        if state["type"] == "tool_use" and state["json"]:
            partial = self._restore_json("".join(state["json"]), state["name"])
            delta = {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": partial}}
            return self._emit("content_block_delta", delta)
        return b""

    def _restore_json(self, partial: str, name: str) -> str:
        try:
            tree = json.loads(partial)
        except ValueError:
            # Not parseable as a whole: restore in place, JSON-escaping each value.
            only, decisions = tool_values(name, partial, self.vault, self.policy)
            for action, reason, kind, n in decisions:
                self.counts[(TOOL, name, action, reason, kind)] += n

            def swap(m):
                ph, value, suffix = resolve(m, self.vault)
                if value is None:
                    self.counts[UNRESOLVED] += 1
                    return m.group(0)
                if only is not None and ph not in only:
                    return m.group(0)
                self.counts[m.group(1)] += 1
                return json.dumps(value + suffix, ensure_ascii=False)[1:-1]

            return PLACEHOLDER.sub(swap, partial)
        restored = restore_tool(name, tree, self.vault, self.policy, self.counts)
        return partial if restored is tree else json.dumps(restored, ensure_ascii=False)

    @staticmethod
    def _emit(name: str | None, data: dict) -> bytes:
        head = f"event: {name}\n" if name else ""
        return f"{head}data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8", "replace")
