"""Point Codex, pi and opencode at keepmum (`keepmum protect`), and run Codex through it once (`keepmum codex`).

Verified Nov 21, 2026 against Codex 0.169 (its source) and Oct 27 against pi 0.84:

- Codex sends its built-in `openai` provider wherever `openai_base_url` says, in ~/.codex/config.toml
  (CODEX_HOME respected), and reads that afresh for every conversation it starts or reopens, in a
  process already running too (an editor's app-server, the app's daemon, a terminal). A conversation
  keeps the provider it started with for good (resuming refuses another), so keepmum points that
  provider rather than selecting one of its own: conversations from before protection went on go
  through keepmum when reopened, and none is stranded when it goes off. One URL serves both logins: a
  ChatGPT login's requests carry its account (`chatgpt-account-id`), and the proxy sends those to
  chatgpt.com/backend-api/codex, the rest to api.openai.com/v1. The built-in provider tries a WebSocket
  first; keepmum answers 426, on which Codex goes on over HTTPS at once.
- A conversation already open when protection goes on keeps its route until it is closed (Codex has
  no reload for it, and restarting Codex would interrupt its work). Codex holds an open conversation's
  rollout file open, so keepmum notes those as it goes on, and status names how many still are.
- Up to 0.1.0a12 keepmum selected a provider of its own (`keepmum`). The background service moves such
  a config to this way as it starts (`codex_upgrade`), and keeps that provider defined, going wherever
  Codex's own goes, for the conversations that started with it.
- pi overrides a built-in provider's base URL in ~/.pi/agent/models.json
  (PI_CODING_AGENT_DIR respected). keepmum sets it for anthropic, openai and openai-codex.

Verified Oct 29, 2026 against opencode 1.19.5 and 2.2.1 (source):

- opencode merges global config.json, opencode.json, then opencode.jsonc from
  $XDG_CONFIG_HOME/opencode (default ~/.config/opencode), later files winning key by key.
  keepmum writes `provider.<id>.options.baseURL` and `headers` into opencode.json, the 1.x shape
  that 2.x still reads, for anthropic and openai; their SDKs then call {base}/v1/messages and
  {base}/openai/v1/responses. An opencode.jsonc or a 2.x `providers` entry that sets its own
  base URL wins and is reported, never edited. keepmum edits only a plain-JSON opencode.json.
- A ChatGPT login in 2.x keeps the configured base URL and sends `chatgpt-account-id`, which
  the proxy routes to chatgpt.com. 1.x replaces the URL with chatgpt.com itself, so its OpenAI
  traffic never reaches keepmum; status says so instead of claiming it is covered.

An agent already pointed at another gateway (Magpie, LiteLLM, a company proxy) gets keepmum in front
of it (gateway.py): Codex's provider (or its own openai_base_url) is pointed at keepmum's route to the
gateway, its line noting the URL it replaced, which `off` puts back; pi's and opencode's base URL
becomes keepmum's route for the gateway, and `off` maps the route back. A provider whose URL keepmum
did not write is otherwise never edited. Codex on a local model (ollama, lmstudio, oss) needs no
protection: nothing leaves this Mac.

pi and opencode get an `x-keepmum-client` header, so the dashboard can tell clients apart (the proxy
drops it before forwarding); Codex is told by its own user agent. Only what keepmum added is ever
removed, and a different base URL the user already set is left alone and reported.
"""

from __future__ import annotations

import json
import os
import shutil
import re
import subprocess
import tomllib
from datetime import datetime
from pathlib import Path

from .gateway import find, is_ours, name_for, origin, parse_route, route_url, unroute
from .protect import ProtectError, _gateways, _read, _write, keepmum_url

CODEX_MARK = "# keepmum protect"
# Up to 0.1.0a12 keepmum selected a provider of its own (`keepmum`). Codex keeps the provider a
# conversation started with for good, so a config that had it keeps its table, in this block, for
# those conversations to resume with: it goes wherever Codex's own provider goes, on or off.
CODEX_BEGIN = "# >>> keepmum protect: Codex conversations started through keepmum's own provider resume with it"
CODEX_END = "# <<< keepmum protect"
_CODEX_BLOCK = "# >>> keepmum protect"  # this version's block, and the one earlier versions wrote
_CODEX_WAS = re.compile(r'^\s*model_provider\s*=\s*"keepmum"\s*# keepmum protect \(was "([^"]+)"\)', re.M)
# A URL keepmum wrote: openai_base_url at the top, or the base_url of the provider Codex selects,
# with the URL it took the place of, if any.
_CODEX_POINTED = re.compile(r'^(?P<indent>\s*)(?P<key>openai_base_url|base_url)\s*=\s*(?P<url>"(?:[^"\\]|\\.)*")\s*'
                            + re.escape(CODEX_MARK) + r'(?: \(was (?P<was>"(?:[^"\\]|\\.)*")\))?\s*$')
_TOML_HEADER = re.compile(r'^\s*\[\s*model_providers\s*\.\s*(?:"([^"]+)"|\'([^\']+)\'|([A-Za-z0-9_-]+))\s*\]\s*(?:#.*)?$')


# --- Codex --------------------------------------------------------------------------

def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def codex_auth_mode() -> str:
    """"chatgpt" or "apikey", from Codex's own auth file (default: chatgpt)."""
    try:
        data = json.loads((codex_home() / "auth.json").read_text())
    except (OSError, ValueError):
        return "chatgpt"
    mode = str(data.get("auth_mode") or "").lower()
    return "apikey" if mode in ("apikey", "api_key") or (not data.get("tokens") and data.get("OPENAI_API_KEY")) else "chatgpt"


def _toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)} = {_toml(v)}" for k, v in value.items()) + "}"
    return json.dumps(value)


def _codex_config() -> Path:
    return codex_home() / "config.toml"


def codex_base_url(port: int) -> str:
    """Where Codex's built-in OpenAI provider goes through keepmum, for either login: a ChatGPT
    login's requests carry its account (chatgpt-account-id), which the proxy sends to chatgpt.com."""
    return f"{keepmum_url(port)}/openai/v1"


def _codex_urls(port: int) -> tuple[str, ...]:
    """keepmum's URLs for Codex on `port`: this version's, and those earlier versions wrote."""
    base = keepmum_url(port)
    return codex_base_url(port), f"{base}/chatgpt/backend-api/codex"


CODEX_LOCAL = ("oss", "ollama", "lmstudio")  # Codex's built-in providers for models on this Mac


def _codex_parse(path: Path, text: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ProtectError(f"{path} is not valid TOML ({e}); fix it first") from e


def _codex_selected(parsed: dict) -> str:
    """The provider Codex sends to: its built-in `openai` unless model_provider names another."""
    return str(parsed.get("model_provider") or "openai")


def _codex_target(parsed: dict) -> tuple[str, str | None]:
    """Which URL decides where Codex goes ("openai_base_url", or a provider's name), and that URL."""
    selected = _codex_selected(parsed)
    if selected == "openai":
        url = parsed.get("openai_base_url")
    else:
        url = ((parsed.get("model_providers") or {}).get(selected) or {}).get("base_url")
    return selected, url if isinstance(url, str) else None


def _strip_codex(text: str) -> str:
    """`text` without what earlier versions wrote around a provider of keepmum's own: its block
    (this version's too), any [model_providers.keepmum] table, and its selection, which goes back to
    the provider it replaced (keepmum's line notes it)."""
    was = _CODEX_WAS.search(text)
    selection = f'model_provider = "{was.group(1)}"' if was else None
    out, skipping, in_keepmum_table = [], False, False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(_CODEX_BLOCK):
            skipping = True
            continue
        if skipping:
            if stripped == CODEX_END:
                skipping = False
            continue
        if stripped.startswith("["):
            in_keepmum_table = stripped == "[model_providers.keepmum]"
        if in_keepmum_table:
            continue
        if re.match(r'\s*model_provider\s*=\s*"keepmum"\s*(?:#.*)?$', line):
            if selection is not None:
                out.append(selection)
                selection = None
            continue
        out.append(line)
    return "\n".join(out).rstrip("\n") + "\n" if out else ""


def _codex_legacy_block(parsed: dict) -> str:
    """keepmum's own provider, for the conversations that started with it: the same destination as
    the provider Codex selects now (keepmum while on, the provider's own while off), over HTTPS."""
    selected, url = _codex_target(parsed)
    if selected == "openai":
        provider = {"name": "keepmum", **({"base_url": url} if url else {}), "wire_api": "responses",
                    "requires_openai_auth": True, "supports_websockets": False}
    else:
        table = (parsed.get("model_providers") or {}).get(selected) or {}
        provider = {"name": "keepmum", **{k: v for k, v in table.items() if k not in ("name", "supports_websockets")},
                    "wire_api": "responses", "supports_websockets": False}
    return "\n".join([CODEX_BEGIN, "[model_providers.keepmum]", *(f"{k} = {_toml(v)}" for k, v in provider.items()),
                      CODEX_END])


def _codex_on(parsed: dict, port: int) -> bool:
    """Codex goes through keepmum on `port`: the provider it selects points there (or, as earlier
    versions set it, it selects keepmum's own)."""
    selected, url = _codex_target(parsed)
    return url is not None and (url in _codex_urls(port) or parse_route(url, port) is not None)


def _key_line(lines: list[str], table: str | None, key: str) -> int | None:
    """The index of `key = …` at the top level (table None) or in [model_providers.<table>]."""
    current: str | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            m = _TOML_HEADER.match(line)
            current = next((g for g in m.groups() if g), "\0") if m else "\0"
            continue
        if current == table and re.match(rf"\s*{re.escape(key)}\s*=", line):
            return i
    return None


def _codex_point(text: str, parsed: dict, port: int, adopt) -> str:
    """`text` with the provider Codex selects pointed at keepmum: openai_base_url for its built-in
    OpenAI provider, or another provider's base_url to keepmum's route in front of its gateway. The
    line notes the URL it took the place of, for `off` to put back."""
    path = _codex_config()
    selected, url = _codex_target(parsed)
    if selected in CODEX_LOCAL:
        raise ProtectError(f"Codex uses a model on this Mac ({selected}): nothing leaves it, so keepmum is not needed")
    table, key = (None, "openai_base_url") if selected == "openai" else (selected, "base_url")
    if table is not None:
        entry = (parsed.get("model_providers") or {}).get(table)
        if not entry:
            raise ProtectError(f"{path} selects model_provider {table!r}, which has no [model_providers.{table}] table")
        if url is None:
            raise ProtectError(f"Codex's provider {table!r} has no base_url keepmum can forward to")
        if entry.get("wire_api", "responses") != "responses":
            raise ProtectError(f"Codex's provider uses wire_api = {entry.get('wire_api')!r} (Chat Completions), "
                               "which keepmum does not scan; set wire_api = \"responses\" if the gateway serves it")
    lines = text.splitlines()
    at = _key_line(lines, table, key)
    marked = _CODEX_POINTED.match(lines[at]) if at is not None else None
    was = json.loads(marked.group("was")) if marked and marked.group("was") else None
    if marked:  # keepmum's already: refresh it (the port may have changed), in front of what it noted
        target = codex_base_url(port) if was is None else (url if parse_route(url, port) else None)
        if target is None:
            if adopt is None:
                raise ProtectError(f"{path}: keepmum's route for Codex is out of date; run `keepmum protect on`")
            target = adopt(was, "Codex")
    elif url is None:
        target = codex_base_url(port)
    elif url in _codex_urls(port) or parse_route(url, port):
        target = url  # pointed at keepmum by hand: keepmum's from now on, and `off` takes it out
    else:
        if adopt is None:
            raise ProtectError(f"{path} already sets {key} " + (f"of model_provider {table!r} " if table else "")
                               + f"to {url}; `keepmum protect on` puts keepmum in front of it")
        target, was = adopt(url, "Codex"), url
    line = f"{key} = {json.dumps(target)}  {CODEX_MARK}" + (f" (was {json.dumps(was)})" if was else "")
    if at is not None:
        lines[at] = (marked.group("indent") if marked else re.match(r"\s*", lines[at]).group(0)) + line
        return "\n".join(lines).rstrip("\n") + "\n"
    if table is not None:  # a base_url in a dotted key or an inline table: not one keepmum can edit in place
        raise ProtectError(f"{path}: keepmum edits a provider's base_url only in a [model_providers.{table}] table; "
                           f"write {table}'s settings that way")
    return _set_top_key(text, key, line)


def _codex_unpoint(text: str) -> str:
    """`text` with every URL keepmum wrote back to what it took the place of, or taken out."""
    out = []
    for line in text.splitlines():
        m = _CODEX_POINTED.match(line)
        if m and m.group("was"):
            out.append(f"{m.group('indent')}{m.group('key')} = {m.group('was')}")
        elif not m:
            out.append(line)
    return "\n".join(out).rstrip("\n") + "\n" if out else ""


def _codex_rewrite(text: str, port: int, adopt, on: bool) -> str:
    """The config with Codex pointed at keepmum (on) or back where it went (off): what earlier
    versions wrote is replaced, and their provider kept, following the one Codex selects."""
    path = _codex_config()
    legacy = "keepmum" in (_codex_parse(path, text).get("model_providers") or {})
    base = _strip_codex(text)
    base = _codex_point(base, _codex_parse(path, base), port, adopt) if on else _codex_unpoint(base)
    if legacy:
        base = base.rstrip("\n") + "\n\n" + _codex_legacy_block(_codex_parse(path, base)) + "\n"
    try:
        tomllib.loads(base)  # never write a file Codex cannot read
    except tomllib.TOMLDecodeError as e:
        raise ProtectError(f"could not update {path}: {e}") from e
    return base


def codex_status(port: int, gateways: dict[str, str] | None = None) -> dict:
    path = _codex_config()
    if not path.exists() and not shutil.which("codex"):
        return {"installed": False, "on": False}
    try:
        text = path.read_text() if path.exists() else ""
        cfg = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return {"installed": True, "on": False, "error": f"{path} is not valid TOML ({e})"}
    gateways = _gateways() if gateways is None else gateways
    selected, url = _codex_target(cfg)
    on = _codex_on(cfg, port)
    out = {"installed": True, "on": on, "config": str(path)}
    if on and parse_route(url, port):
        out["via"] = unroute(url, port, gateways)
    if not on and url and selected != "keepmum":
        out["other"], out["other_url"] = selected, url
    elif not on and selected not in ("openai", "keepmum"):
        out["other"] = selected
    pointed = any(_CODEX_POINTED.match(line) for line in text.splitlines()) or selected == "keepmum"
    if pointed and not on:
        out["warning"] = "keepmum's port changed since Codex was pointed at it; run `keepmum protect on --client codex`"
    elif on and parse_route(url, port) and not out.get("via"):
        out["warning"] = "its gateway is no longer in keepmum's config; run `keepmum protect on --client codex`"
    elif on and (still := _codex_still_open()):
        n = len(still)
        out["warning"] = (f"{n} Codex conversation{'s' if n > 1 else ''} open since before protection went on "
                          f"still go{'es' if n == 1 else ''} straight to OpenAI until closed; every conversation "
                          "started or reopened since goes through keepmum")
    return out


# --- Conversations open when Codex is switched on ----------------------------------------------

def _codex_pids() -> list[int]:
    """Every process running Codex itself (the CLI, an editor's or the app's app-server)."""
    try:
        res = subprocess.run(["ps", "-A", "-o", "pid=,args="], capture_output=True, text=True, timeout=5,
                             env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for line in res.stdout.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit() and re.match(r"(?:\S*/)?codex(?=\s|$)", args.strip()):
            pids.append(int(pid))
    return pids


def _codex_open(pids: list[int] | None = None) -> set[tuple[int, int, str]]:
    """(pid, fd, rollout) for every Codex conversation loaded now. Codex holds a conversation's
    rollout file (sessions/…/rollout-*.jsonl) open from its first entry until the conversation
    unloads, which a conversation no window shows does when idle. Empty if lsof cannot say."""
    pids = _codex_pids() if pids is None else pids
    if not pids:
        return set()
    try:
        res = subprocess.run(["lsof", "-n", "-P", "-a", "-p", ",".join(map(str, pids)), "-Fpfn"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return set()
    found, pid, fd = set(), 0, -1
    for line in res.stdout.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid = int(value) if value.isdigit() else 0
        elif tag == "f":
            fd = int(value) if value.isdigit() else -1
        elif tag == "n" and "/sessions/" in value and Path(value).name.startswith("rollout-") and value.endswith(".jsonl"):
            found.add((pid, fd, value))
    return found


def _codex_open_file() -> Path:
    from .config import config_file

    return config_file().parent / "codex-open.json"


def _codex_note_open() -> None:
    """Note the conversations open in Codex as it is switched on: they keep the route they started
    with (Codex reads its config as each conversation starts or reopens), so they still go straight
    to OpenAI until closed, and status says so while they stay open."""
    path, open_now = _codex_open_file(), sorted(_codex_open())
    try:
        if open_now:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"since": datetime.now().astimezone().isoformat(), "open": open_now}))
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _codex_forget_open() -> None:
    try:
        _codex_open_file().unlink(missing_ok=True)
    except OSError:
        pass


def _codex_still_open() -> list[tuple[int, int, str]]:
    """Those noted conversations still open in the same process; the note goes once none is."""
    try:
        noted = {tuple(x) for x in json.loads(_codex_open_file().read_text())["open"]}
    except (OSError, ValueError, KeyError, TypeError):
        return []
    still = sorted(noted & _codex_open(sorted({pid for pid, _, _ in noted})))
    if not still:
        _codex_forget_open()
    return still


def _set_top_key(text: str, key: str, new: str) -> str:
    """`text` with its top-level `key = ...` line replaced by `new` where it stood, so `off` can
    put the file back as it was; `new` goes first if there is none (top-level keys come before
    the first table)."""
    out, top, placed = [], True, False
    for line in text.splitlines():
        if line.lstrip().startswith("["):
            top = False
        if top and re.match(rf"\s*{re.escape(key)}\s*=", line):
            if not placed:
                out.append(new)
                placed = True
            continue
        out.append(line)
    if not placed:
        out.insert(0, new)
    return "\n".join(out).rstrip("\n") + "\n"


def codex_enable(port: int, adopt=None) -> dict:
    """Point the provider Codex selects at keepmum: openai_base_url for its built-in OpenAI provider,
    or, if Codex selects another gateway, that provider's base_url to the route `adopt(url, who)` gives
    (gateway.Adopter). Every conversation Codex starts or reopens from then on goes through keepmum."""
    path = _codex_config()
    text = path.read_text() if path.exists() else ""
    was_on = _codex_on(_codex_parse(path, text), port)
    new = _codex_rewrite(text, port, adopt, on=True)
    if new != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_text(path, new)
    if not was_on:
        _codex_note_open()
    return codex_status(port, getattr(adopt, "gateways", None))


def codex_disable(port: int) -> dict:
    """Take keepmum out: each URL it wrote goes back to the one it replaced, or out."""
    path = _codex_config()
    if path.exists():
        text = path.read_text()
        new = _codex_rewrite(text, port, None, on=False)
        if new != text:
            _write_text(path, new)
    _codex_forget_open()
    return codex_status(port)


def codex_upgrade(port: int, adopt=None) -> dict | None:
    """A config an earlier version pointed at keepmum (it selected keepmum's own provider) moved to
    this version's way, protection unchanged; None if there was nothing to move. `keepmum serve`
    runs it as it starts, so updating keepmum is enough."""
    path = _codex_config()
    try:
        text = path.read_text()
    except OSError:
        return None
    if not re.search(r'^\s*model_provider\s*=\s*"keepmum"', text, re.M):
        return None
    return codex_enable(port, adopt)


def _codex_route(url: str, port: int, gateways: dict[str, str], extra: dict[str, str]) -> str:
    """keepmum's route in front of `url`, adding to `extra` a gateway the config does not have yet."""
    url = url.rstrip("/")
    hit = find(url, gateways)
    if hit is None:
        name = name_for(url, gateways)
        extra[name] = origin(url)
        hit = find(url, {**gateways, **extra})
    return route_url(port, *hit)


def codex_run_args(port: int, gateways: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """`-c` overrides that send one Codex run through keepmum, its config file left alone, and the
    gateways that run needs beyond the config's: in front of the one Codex's config points at."""
    path = _codex_config()
    parsed = _codex_parse(path, path.read_text() if path.exists() else "")
    selected, url = _codex_target(parsed)
    if selected in CODEX_LOCAL:
        raise ProtectError(f"Codex uses a model on this Mac ({selected}): nothing leaves it, so run `codex`")
    extra: dict[str, str] = {}
    if selected in ("openai", "keepmum"):
        mine = url is None or selected == "keepmum" or url in _codex_urls(port) or parse_route(url, port)
        target = codex_base_url(port) if mine else _codex_route(url, port, gateways, extra)
        return ["-c", f"openai_base_url={_toml(target)}"], extra
    if url is None:
        raise ProtectError(f"Codex's provider {selected!r} has no base_url keepmum can forward to")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", selected):
        raise ProtectError(f"keepmum cannot name Codex's provider {selected!r} in a `-c` override")
    target = url if parse_route(url, port) else _codex_route(url, port, gateways, extra)
    return ["-c", f"model_providers.{selected}.base_url={_toml(target)}"], extra


def codex_args(port: int, provider: dict | None = None) -> list[str]:
    """`-c` overrides that send one Codex run through keepmum: its built-in OpenAI provider pointed
    there, or (`provider`) a provider of keepmum's own, as the e2e suite passes for API keys."""
    if provider is None:
        return ["-c", f"openai_base_url={_toml(codex_base_url(port))}"]
    out = ["-c", 'model_provider="keepmum"']
    for key, value in provider.items():
        out += ["-c", f"model_providers.keepmum.{key}={_toml(value)}"]
    return out


def _write_text(path: Path, text: str) -> None:
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".keepmum-backup"))
    tmp = path.with_name(path.name + ".keepmum-tmp")
    tmp.write_text(text)
    if path.exists():
        os.chmod(tmp, path.stat().st_mode & 0o777)
    tmp.replace(path)


# --- pi -----------------------------------------------------------------------------

def pi_dir() -> Path:
    return Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent")


def pi_providers(port: int) -> dict[str, str]:
    base = keepmum_url(port)
    return {"anthropic": base, "openai": f"{base}/openai/v1", "openai-codex": f"{base}/chatgpt/backend-api"}


# pi's and opencode's providers keepmum can put itself in front of another gateway for: chatgpt.com
# (openai-codex) is a subscription no gateway serves.
GATEWAY_APIS = ("anthropic", "openai")


def _status_urls(want: dict[str, str], urls: dict, port: int, gateways: dict[str, str] | None) -> dict:
    """on, providers, via and other, from each provider's base URL now."""
    gateways = _gateways() if gateways is None else gateways
    ours = sorted(p for p, url in want.items() if urls.get(p) == url or parse_route(urls.get(p), port))
    others = sorted(p for p in want if p not in ours and urls.get(p))
    via = {p: unroute(urls[p], port, gateways) for p in ours if parse_route(urls[p], port)}
    out = {"on": bool(ours), "providers": ours, **({"other": others} if others else {})}
    if via:
        out["via"] = ", ".join(sorted({v or "a gateway no longer in keepmum's config" for v in via.values()}))
        if len(via) < len(ours):  # the others go to their provider directly, through keepmum
            out["via_providers"] = sorted(via)
    if others:
        out["other_url"] = ", ".join(sorted({str(urls[p]) for p in others}))
    return out


def via_phrase(st: dict) -> str:
    """How the protect and doctor lines end for an agent in front of a gateway."""
    if st.get("via_providers"):
        return f"; {', '.join(st['via_providers'])} then goes to {st['via']}"
    return f", then {st['via']}"


def _point(entry: dict, key: str, want: str, port: int, adopt, who: str, gateway_ok: bool) -> bool:
    """Point one provider's base URL (entry[key]) at keepmum, or in front of the gateway it holds.
    False: left alone (the user's own URL, and no adopt, or not a gateway API)."""
    current = entry.get(key)
    if current is None or current == want or parse_route(current, port):
        entry[key] = current if current is not None and parse_route(current, port) else want
        return True
    if adopt is None or not gateway_ok or not isinstance(current, str):
        return False
    entry[key] = adopt(current, who)
    return True


def _unpoint(entry: dict, key: str, want: str, port: int, gateways: dict[str, str]) -> bool:
    """Undo `_point`: the gateway's URL back, or keepmum's value removed. False: not keepmum's."""
    current = entry.get(key)
    if current == want:
        entry.pop(key)
        return True
    if parse_route(current, port):
        back = unroute(current, port, gateways)
        if back:
            entry[key] = back
        else:
            entry.pop(key)
        return True
    return False


def pi_status(port: int, gateways: dict[str, str] | None = None) -> dict:
    path = pi_dir() / "models.json"
    if not path.exists() and not shutil.which("pi"):
        return {"installed": False, "on": False}
    try:
        providers = _read(path).get("providers") or {}
    except ProtectError as e:
        return {"installed": True, "on": False, "error": str(e)}
    urls = {p: (providers.get(p) or {}).get("baseUrl") for p in pi_providers(port)}
    return {"installed": True, **_status_urls(pi_providers(port), urls, port, gateways), "config": str(path)}


def pi_enable(port: int, adopt=None) -> dict:
    path = pi_dir() / "models.json"
    data = _read(path)
    providers = data.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise ProtectError(f"{path}: providers is not an object")
    for name, url in pi_providers(port).items():
        entry = providers.setdefault(name, {})
        if _point(entry, "baseUrl", url, port, adopt, f"pi's {name} provider", name in GATEWAY_APIS):
            entry.setdefault("headers", {})["x-keepmum-client"] = "pi"
        elif not entry:
            providers.pop(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, data)
    return pi_status(port, getattr(adopt, "gateways", None))


def pi_disable(port: int) -> dict:
    path = pi_dir() / "models.json"
    if not path.exists():
        return pi_status(port)
    data = _read(path)
    providers = data.get("providers") or {}
    changed = False
    gateways = _gateways()
    for name, url in pi_providers(port).items():
        entry = providers.get(name)
        if not isinstance(entry, dict) or not _unpoint(entry, "baseUrl", url, port, gateways):
            continue
        headers = entry.get("headers") or {}
        headers.pop("x-keepmum-client", None)
        if not headers:
            entry.pop("headers", None)
        if not entry:
            providers.pop(name)
        changed = True
    if changed:
        if not providers:
            data.pop("providers", None)
        _write(path, data)
    return pi_status(port)


# --- opencode -----------------------------------------------------------------------

OPENCODE_FILES = ("config.json", "opencode.json", "opencode.jsonc")  # merge order: later wins


def opencode_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode"


def opencode_providers(port: int) -> dict[str, str]:
    base = keepmum_url(port)
    return {"anthropic": f"{base}/v1", "openai": f"{base}/openai/v1"}


def _jsonc(text: str):
    """JSON with // and /* */ comments and trailing commas, as opencode reads it."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))


def _opencode_base(data: dict, provider: str):
    """The base URL one config file sets for a provider: 1.x `provider` or 2.x `providers`."""
    v1 = ((data.get("provider") or {}).get(provider) or {})
    v2 = ((data.get("providers") or {}).get(provider) or {})
    url = ((v2.get("settings") or {}) if isinstance(v2, dict) else {}).get("baseURL")
    if url is None and isinstance(v1, dict):
        url = (v1.get("options") or {}).get("baseURL") or v1.get("api")
    return url


def _opencode_effective(port: int) -> dict[str, str | None]:
    """Each provider's base URL after opencode merges its global files."""
    urls = {p: None for p in opencode_providers(port)}
    for name in OPENCODE_FILES:
        path = opencode_dir() / name
        if not path.exists():
            continue
        try:
            data = _jsonc(path.read_text())
        except ValueError as e:
            raise ProtectError(f"{path} is not valid JSON ({e}); fix it first") from e
        if not isinstance(data, dict):
            raise ProtectError(f"{path} does not hold a config object")
        for key in ("provider", "providers"):
            if not isinstance(data.get(key) or {}, dict):
                raise ProtectError(f"{path}: `{key}` should be an object of providers; fix it first")
        for p in urls:
            url = _opencode_base(data, p)
            if url is not None:
                urls[p] = url
    return urls


_version_cache: dict = {}


def opencode_major() -> int | None:
    """opencode's major version, from `opencode --version`, cached per binary."""
    exe = shutil.which("opencode")
    if not exe:
        return None
    try:
        key = (exe, os.stat(exe).st_mtime)
        if key not in _version_cache:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5).stdout
            found = re.search(r"(\d+)\.\d+", out)
            _version_cache.clear()
            _version_cache[key] = int(found.group(1)) if found else None
        return _version_cache[key]
    except (OSError, subprocess.SubprocessError):
        return None


def _opencode_chatgpt_login() -> bool:
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    try:
        auth = json.loads((data_home / "opencode" / "auth.json").read_text())
    except (OSError, ValueError):
        return False
    return isinstance(auth, dict) and (auth.get("openai") or {}).get("type") == "oauth"


def opencode_status(port: int, gateways: dict[str, str] | None = None) -> dict:
    folder = opencode_dir()
    if not any((folder / n).exists() for n in OPENCODE_FILES) and not shutil.which("opencode"):
        return {"installed": False, "on": False}
    try:
        urls = _opencode_effective(port)
    except ProtectError as e:
        return {"installed": True, "on": False, "error": str(e)}
    want = opencode_providers(port)
    out = {"installed": True, **_status_urls(want, urls, port, gateways), "config": str(folder / "opencode.json")}
    ours = out["providers"]
    if "openai" in ours and opencode_major() == 1 and _opencode_chatgpt_login():
        out["warning"] = "opencode 1.x sends a ChatGPT login straight to chatgpt.com, around keepmum; update to 2.x"
    return out


def opencode_enable(port: int, adopt=None) -> dict:
    path = opencode_dir() / "opencode.json"
    urls = _opencode_effective(port)
    try:
        data = json.loads(path.read_text()) if path.exists() else {"$schema": "https://opencode.ai/config.json"}
    except ValueError as e:
        raise ProtectError(f"{path} has comments or is not plain JSON; keepmum will not rewrite it") from e
    providers = data.setdefault("provider", {})
    if not isinstance(providers, dict):
        raise ProtectError(f"{path}: provider is not an object")
    for name, url in opencode_providers(port).items():
        entry = providers.get(name) if isinstance(providers.get(name), dict) else {}
        options = entry.get("options") if isinstance(entry.get("options"), dict) else {}
        # Only a base URL this file sets is ours to change: one from opencode.jsonc, a 2.x
        # `providers` entry or `api` wins over it and is left alone, reported by status.
        if urls[name] not in (None, url) and options.get("baseURL") != urls[name]:
            continue
        if not _point(options, "baseURL", url, port, adopt, f"opencode's {name} provider", True):
            continue
        options.setdefault("headers", {})["x-keepmum-client"] = "opencode"
        entry["options"] = options
        providers[name] = entry
    _write(path, data)
    return opencode_status(port, getattr(adopt, "gateways", None))


def opencode_disable(port: int) -> dict:
    path = opencode_dir() / "opencode.json"
    if not path.exists():
        return opencode_status(port)
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise ProtectError(f"{path} has comments or is not plain JSON; keepmum will not rewrite it") from e
    providers = data.get("provider") or {}
    changed = False
    gateways = _gateways()
    for name, url in opencode_providers(port).items():
        entry = providers.get(name)
        options = entry.get("options") if isinstance(entry, dict) else None
        if not isinstance(options, dict) or not _unpoint(options, "baseURL", url, port, gateways):
            continue
        headers = options.get("headers") or {}
        headers.pop("x-keepmum-client", None)
        if not headers:
            options.pop("headers", None)
        if not options:
            entry.pop("options")
        if not entry:
            providers.pop(name)
        changed = True
    if changed:
        if not providers:
            data.pop("provider", None)
        _write(path, data)
    return opencode_status(port)


# --- All clients ----------------------------------------------------------------------

CLIENTS = ("claude", "codex", "pi", "opencode")


def protect_clients(action: str, port: int, only: list[str] | None = None, adopt=None) -> dict[str, dict]:
    """Run on, off or status for each client; one failing never stops the others. With `adopt`
    (gateway.Adopter), `on` puts keepmum in front of a gateway an agent already uses."""
    from . import protect

    def claude(fn, *args):
        st = fn(port, *args).as_dict()
        other = st.get("base_url") if not st.get("on") and st.get("base_url") else None
        return {"installed": True, **st, **({"other": other, "other_url": other} if other else {})}

    table = {
        "claude": {"on": lambda: claude(protect.enable, adopt), "off": lambda: claude(protect.disable),
                   "status": lambda: claude(protect.status)},
        "codex": {"on": lambda: codex_enable(port, adopt), "off": lambda: codex_disable(port), "status": lambda: codex_status(port)},
        "pi": {"on": lambda: pi_enable(port, adopt), "off": lambda: pi_disable(port), "status": lambda: pi_status(port)},
        "opencode": {"on": lambda: opencode_enable(port, adopt), "off": lambda: opencode_disable(port),
                     "status": lambda: opencode_status(port)},
    }
    out = {}
    gateways = _gateways()
    for name in only or CLIENTS:
        try:
            installed = name == "claude" or table[name]["status"]().get("installed")
            out[name] = table[name][action]() if installed or action == "status" else {"installed": False, "on": False}
        except ProtectError as e:
            out[name] = {"installed": True, "on": False, "error": str(e)}
        # Straight to a gateway keepmum is in front of: most likely the gateway's app wrote its
        # own address back (switching a model, say), and the agent now goes around keepmum.
        around = [u.strip() for u in str(out[name].get("other_url") or "").split(",") if find(u.strip(), gateways)]
        if around:
            out[name]["around"] = ", ".join(around)
    return out
