"""Point Codex, pi and opencode at keepmum (`keepmum protect`), and run Codex through it once (`keepmum codex`).

Verified Oct 27, 2026 against Codex 0.147 and pi 0.84:

- Codex cannot override its built-in `openai` provider, so keepmum adds a `keepmum` provider in
  ~/.codex/config.toml (CODEX_HOME respected) and selects it with a top-level
  `model_provider`. `requires_openai_auth` keeps the user's ChatGPT or API-key login;
  `supports_websockets = false` skips the WebSocket transport keepmum cannot scan, so Codex goes
  straight to HTTPS. A ChatGPT login is served by chatgpt.com/backend-api/codex, an API key
  by api.openai.com/v1.
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
of it (gateway.py): Codex's own provider block is copied into keepmum's, base URL aside, and the
provider it selected is noted on keepmum's line, so `off` selects it again; pi's and opencode's
base URL becomes keepmum's route for the gateway, and `off` maps the route back. A provider whose
URL keepmum did not write is otherwise never edited. Codex on a local model (ollama, lmstudio, oss)
needs no protection: nothing leaves this Mac.

All three get an `x-keepmum-client` header, so the dashboard can tell clients apart; the proxy drops
it before forwarding. Only what keepmum added is ever removed, and a different base URL the user
already set is left alone and reported.
"""

from __future__ import annotations

import json
import os
import shutil
import re
import subprocess
import tomllib
from pathlib import Path

from .gateway import find, is_ours, name_for, origin, parse_route, route_url, unroute
from .protect import ProtectError, _gateways, _read, _write, keepmum_url

CODEX_MARK = "# keepmum protect"
CODEX_BEGIN = "# >>> keepmum protect: routes Codex through keepmum; `keepmum protect off` removes this block"
CODEX_END = "# <<< keepmum protect"


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


def codex_base_url(port: int) -> str:
    base = keepmum_url(port)
    return f"{base}/openai/v1" if codex_auth_mode() == "apikey" else f"{base}/chatgpt/backend-api/codex"


def codex_provider(port: int) -> dict:
    return {"name": "keepmum", "base_url": codex_base_url(port), "wire_api": "responses",
            "requires_openai_auth": True, "supports_websockets": False,
            "http_headers": {"x-keepmum-client": "codex"}}


CODEX_LOCAL = ("oss", "ollama", "lmstudio")  # Codex's built-in providers for models on this Mac
_CODEX_WAS = re.compile(r'^\s*model_provider\s*=\s*"keepmum"\s*# keepmum protect \(was "([^"]+)"\)', re.M)


def _codex_in_front(table: dict, route: str) -> dict:
    """keepmum's Codex provider in front of the user's own: their key, headers and auth as they
    were, the base URL keepmum's route to the same place, and HTTPS only."""
    if table.get("wire_api", "responses") != "responses":
        raise ProtectError(f"Codex's provider uses wire_api = {table.get('wire_api')!r} (Chat Completions), "
                           "which keepmum does not scan; set wire_api = \"responses\" if the gateway serves it")
    out = {k: v for k, v in table.items() if k not in ("name", "base_url", "supports_websockets", "http_headers")}
    return {"name": "keepmum", "base_url": route, **out, "wire_api": "responses", "supports_websockets": False,
            "http_headers": {**(table.get("http_headers") or {}), "x-keepmum-client": "codex"}}


def _codex_source(parsed: dict, text: str) -> str | None:
    """The provider Codex used before keepmum (the one keepmum's line notes), or that it selects now."""
    current = parsed.get("model_provider")
    if current == "keepmum":
        m = _CODEX_WAS.search(text)
        return m.group(1) if m else None
    return current


def codex_run_provider(port: int, gateways: dict[str, str]) -> tuple[dict, dict[str, str]]:
    """The keepmum provider for one Codex run on `port`, and gateways that run needs beyond the
    config's: in front of the provider Codex's config selects, if that is another gateway."""
    path = _codex_config()
    text = path.read_text() if path.exists() else ""
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ProtectError(f"{path} is not valid TOML ({e}); fix it first") from e
    source = _codex_source(parsed, text)
    if source in CODEX_LOCAL:
        raise ProtectError(f"Codex uses a model on this Mac ({source}): nothing leaves it, so run `codex`")
    table = (parsed.get("model_providers") or {}).get(source) if source not in (None, "openai") else None
    if not table:
        return codex_provider(port), {}
    if not isinstance(table.get("base_url"), str):
        raise ProtectError(f"Codex's provider {source!r} has no base_url keepmum can forward to")
    url = table["base_url"].rstrip("/")
    extra = {}
    hit = find(url, gateways)
    if hit is None:
        name = name_for(url, gateways)
        extra[name] = origin(url)
        hit = find(url, {**gateways, **extra})
    return _codex_in_front(table, route_url(port, *hit)), extra


def codex_args(port: int, provider: dict | None = None) -> list[str]:
    """`-c` overrides that send one Codex run through keepmum, leaving its config file alone."""
    out = ["-c", 'model_provider="keepmum"']
    for key, value in (provider or codex_provider(port)).items():
        out += ["-c", f"model_providers.keepmum.{key}={_toml(value)}"]
    return out


def _toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)} = {_toml(v)}" for k, v in value.items()) + "}"
    return json.dumps(value)


def _codex_config() -> Path:
    return codex_home() / "config.toml"


def _strip_codex(text: str, drop_keepmum_provider: bool = False, selection: str | None = None) -> str:
    """`text` without keepmum's block and its model_provider line; `selection` takes that line's place."""
    out, skipping, in_keepmum_table = [], False, False
    for line in text.splitlines():
        if line.strip() == CODEX_BEGIN:
            skipping = True
            continue
        if skipping:
            if line.strip() == CODEX_END:
                skipping = False
            continue
        stripped = line.strip()
        if drop_keepmum_provider and stripped.startswith("["):
            in_keepmum_table = stripped == "[model_providers.keepmum]"
        if in_keepmum_table:
            continue
        if (CODEX_MARK in line and line.lstrip().startswith("model_provider")) or (
                drop_keepmum_provider and re.match(r'\s*model_provider\s*=\s*"keepmum"\s*(?:#.*)?$', line)):
            if selection is not None:
                out.append(selection)
                selection = None
            continue
        out.append(line)
    return "\n".join(out).rstrip("\n") + "\n" if out else ""


def codex_status(port: int, gateways: dict[str, str] | None = None) -> dict:
    path = _codex_config()
    if not path.exists() and not shutil.which("codex"):
        return {"installed": False, "on": False}
    try:
        text = path.read_text() if path.exists() else ""
        cfg = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return {"installed": True, "on": False, "error": f"{path} is not valid TOML ({e})"}
    providers = cfg.get("model_providers") or {}
    provider = providers.get("keepmum") or {}
    base = provider.get("base_url")
    via = unroute(base, port, _gateways() if gateways is None else gateways) if parse_route(base, port) else None
    on = cfg.get("model_provider") == "keepmum" and (base == codex_base_url(port) or via is not None)
    current = cfg.get("model_provider")
    other = current not in (None, "keepmum") and current
    out = {"installed": True, "on": on, "config": str(path), **({"via": via} if via else {})}
    if other:
        out["other"] = other
        url = (providers.get(other) or {}).get("base_url")
        if isinstance(url, str):
            out["other_url"] = url
    if current == "keepmum" and not on:
        out["warning"] = ("its gateway is no longer in keepmum's config; run `keepmum protect on --client codex`"
                          if parse_route(base, port) else
                          "Codex login mode or keepmum port changed; run `keepmum protect on --client codex` to update its URL")
    return out


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
    """Select a `keepmum` provider. If Codex selects another gateway, `adopt(url, who)` gives
    keepmum's route to it (gateway.Adopter), and keepmum's line notes the provider it replaced."""
    path = _codex_config()
    text = path.read_text() if path.exists() else ""
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ProtectError(f"{path} is not valid TOML ({e}); fix it first") from e
    source = _codex_source(parsed, text)
    if source in CODEX_LOCAL:
        raise ProtectError(f"Codex uses a model on this Mac ({source}): nothing leaves it, so keepmum is not needed")
    table = (parsed.get("model_providers") or {}).get(source) if source not in (None, "openai") else None
    if source not in (None, "openai") and not table:
        raise ProtectError(f"{path} selects model_provider {source!r}, which has no [model_providers.{source}] table")
    if table:
        if not isinstance(table.get("base_url"), str):
            raise ProtectError(f"Codex's provider {source!r} has no base_url keepmum can forward to")
        if adopt is None:
            raise ProtectError(f"{path} already selects model_provider {source!r}; `keepmum protect on` puts keepmum in front of it")
        _codex_in_front(table, "")  # refuse Chat Completions before anything is added to the config
        provider = _codex_in_front(table, adopt(table["base_url"], "Codex"))
    else:
        provider = codex_provider(port)
    block = [CODEX_BEGIN, "[model_providers.keepmum]"]
    block += [f"{k} = {_toml(v)}" for k, v in provider.items()]
    block.append(CODEX_END)
    line = 'model_provider = "keepmum"  ' + CODEX_MARK + (f' (was "{source}")' if source else "")
    base = _set_top_key(_strip_codex(text, drop_keepmum_provider=True, selection=line), "model_provider", line)
    new = base + "\n" + "\n".join(block) + "\n"
    try:
        tomllib.loads(new)  # never write a file Codex cannot read
    except tomllib.TOMLDecodeError as e:
        raise ProtectError(f"could not update {path}: {e}") from e
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text(path, new)
    return codex_status(port, getattr(adopt, "gateways", None))


def codex_disable(port: int) -> dict:
    """Take keepmum out: Codex selects again the provider it had (keepmum's line notes it)."""
    path = _codex_config()
    if path.exists():
        text = path.read_text()
        m = _CODEX_WAS.search(text)
        new = _strip_codex(text, selection=f'model_provider = "{m.group(1)}"' if m else None)
        if new != text:
            try:
                tomllib.loads(new)
            except tomllib.TOMLDecodeError as e:
                raise ProtectError(f"could not update {path}: {e}") from e
            _write_text(path, new)
    return codex_status(port)


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
