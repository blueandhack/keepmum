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

from .protect import ProtectError, _read, _write, keepmum_url

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


def codex_args(port: int) -> list[str]:
    """`-c` overrides that send one Codex run through keepmum, leaving its config file alone."""
    out = ["-c", 'model_provider="keepmum"']
    for key, value in codex_provider(port).items():
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


def _strip_codex(text: str, drop_keepmum_provider: bool = False) -> str:
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
        if line.rstrip().endswith(CODEX_MARK) and line.lstrip().startswith("model_provider"):
            continue
        if drop_keepmum_provider and re.match(r'\s*model_provider\s*=\s*"keepmum"\s*(?:#.*)?$', line):
            continue
        out.append(line)
    return "\n".join(out).rstrip("\n") + "\n" if out else ""


def codex_status(port: int) -> dict:
    path = _codex_config()
    if not path.exists() and not shutil.which("codex"):
        return {"installed": False, "on": False}
    try:
        cfg = tomllib.loads(path.read_text()) if path.exists() else {}
    except tomllib.TOMLDecodeError as e:
        return {"installed": True, "on": False, "error": f"{path} is not valid TOML ({e})"}
    provider = (cfg.get("model_providers") or {}).get("keepmum") or {}
    on = cfg.get("model_provider") == "keepmum" and provider.get("base_url") == codex_base_url(port)
    other = cfg.get("model_provider") not in (None, "keepmum") and cfg.get("model_provider")
    warning = ("Codex login mode or keepmum port changed; run `keepmum protect on --client codex` to update its URL"
               if cfg.get("model_provider") == "keepmum" and not on else None)
    return {"installed": True, "on": on, "config": str(path),
            **({"other": other} if other else {}), **({"warning": warning} if warning else {})}


def codex_enable(port: int) -> dict:
    path = _codex_config()
    text = path.read_text() if path.exists() else ""
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ProtectError(f"{path} is not valid TOML ({e}); fix it first") from e
    current = parsed.get("model_provider")
    if current not in (None, "keepmum") and CODEX_MARK not in text:
        raise ProtectError(f"{path} already selects model_provider {current!r}; keepmum leaves it alone")
    base = _strip_codex(text, drop_keepmum_provider=True)
    block = [CODEX_BEGIN, "[model_providers.keepmum]"]
    block += [f"{k} = {_toml(v)}" for k, v in codex_provider(port).items()]
    block.append(CODEX_END)
    new = f'model_provider = "keepmum"  {CODEX_MARK}\n' + base + ("\n" if base else "") + "\n".join(block) + "\n"
    try:
        tomllib.loads(new)  # never write a file Codex cannot read
    except tomllib.TOMLDecodeError as e:
        raise ProtectError(f"could not update {path}: {e}") from e
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text(path, new)
    return codex_status(port)


def codex_disable(port: int) -> dict:
    path = _codex_config()
    if path.exists():
        text = path.read_text()
        new = _strip_codex(text)
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


def pi_status(port: int) -> dict:
    path = pi_dir() / "models.json"
    if not path.exists() and not shutil.which("pi"):
        return {"installed": False, "on": False}
    try:
        providers = _read(path).get("providers") or {}
    except ProtectError as e:
        return {"installed": True, "on": False, "error": str(e)}
    ours = {p for p, url in pi_providers(port).items() if (providers.get(p) or {}).get("baseUrl") == url}
    others = sorted(p for p in pi_providers(port) if p not in ours and (providers.get(p) or {}).get("baseUrl"))
    return {"installed": True, "on": bool(ours), "providers": sorted(ours), "config": str(path),
            **({"other": others} if others else {})}


def pi_enable(port: int) -> dict:
    path = pi_dir() / "models.json"
    data = _read(path)
    providers = data.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise ProtectError(f"{path}: providers is not an object")
    for name, url in pi_providers(port).items():
        entry = providers.setdefault(name, {})
        if entry.get("baseUrl") not in (None, url):
            continue  # the user's own proxy for this provider: left alone, reported by status
        entry["baseUrl"] = url
        entry.setdefault("headers", {})["x-keepmum-client"] = "pi"
    path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, data)
    return pi_status(port)


def pi_disable(port: int) -> dict:
    path = pi_dir() / "models.json"
    if not path.exists():
        return pi_status(port)
    data = _read(path)
    providers = data.get("providers") or {}
    changed = False
    for name, url in pi_providers(port).items():
        entry = providers.get(name)
        if not isinstance(entry, dict) or entry.get("baseUrl") != url:
            continue
        entry.pop("baseUrl")
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


def opencode_status(port: int) -> dict:
    folder = opencode_dir()
    if not any((folder / n).exists() for n in OPENCODE_FILES) and not shutil.which("opencode"):
        return {"installed": False, "on": False}
    try:
        urls = _opencode_effective(port)
    except ProtectError as e:
        return {"installed": True, "on": False, "error": str(e)}
    want = opencode_providers(port)
    ours = sorted(p for p, url in want.items() if urls[p] == url)
    others = sorted(p for p in want if p not in ours and urls[p])
    out = {"installed": True, "on": bool(ours), "providers": ours, "config": str(folder / "opencode.json"),
           **({"other": others} if others else {})}
    if "openai" in ours and opencode_major() == 1 and _opencode_chatgpt_login():
        out["warning"] = "opencode 1.x sends a ChatGPT login straight to chatgpt.com, around keepmum; update to 2.x"
    return out


def opencode_enable(port: int) -> dict:
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
        if urls[name] not in (None, url):
            continue  # the user's own gateway for this provider: left alone, reported by status
        entry = providers.setdefault(name, {})
        options = entry.setdefault("options", {})
        options["baseURL"] = url
        options.setdefault("headers", {})["x-keepmum-client"] = "opencode"
    _write(path, data)
    return opencode_status(port)


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
    for name, url in opencode_providers(port).items():
        entry = providers.get(name)
        options = entry.get("options") if isinstance(entry, dict) else None
        if not isinstance(options, dict) or options.get("baseURL") != url:
            continue
        options.pop("baseURL")
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


def protect_clients(action: str, port: int, only: list[str] | None = None) -> dict[str, dict]:
    """Run on, off or status for each client; one failing never stops the others."""
    from . import protect

    def claude(fn):
        st = fn(port).as_dict()
        other = st.get("base_url") if not st.get("on") and st.get("base_url") else None
        return {"installed": True, **st, **({"other": other} if other else {})}

    table = {
        "claude": {"on": lambda: claude(protect.enable), "off": lambda: claude(protect.disable), "status": lambda: claude(protect.status)},
        "codex": {"on": lambda: codex_enable(port), "off": lambda: codex_disable(port), "status": lambda: codex_status(port)},
        "pi": {"on": lambda: pi_enable(port), "off": lambda: pi_disable(port), "status": lambda: pi_status(port)},
        "opencode": {"on": lambda: opencode_enable(port), "off": lambda: opencode_disable(port),
                     "status": lambda: opencode_status(port)},
    }
    out = {}
    for name in only or CLIENTS:
        try:
            installed = name == "claude" or table[name]["status"]().get("installed")
            out[name] = table[name][action]() if installed or action == "status" else {"installed": False, "on": False}
        except ProtectError as e:
            out[name] = {"installed": True, "on": False, "error": str(e)}
    return out
