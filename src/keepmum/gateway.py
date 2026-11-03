"""Other gateways behind keepmum (Magpie, LiteLLM, a company proxy): agent -> keepmum -> gateway -> provider.

Each gateway is a named route, config [gateways]: `local-3425 = "http://127.0.0.1:3425"`. An agent
already pointed at a gateway is pointed at keepmum's route for it, with the same path after it:
Claude Code's http://127.0.0.1:3425 becomes http://127.0.0.1:7788/gateway/local-3425, and Codex's
http://127.0.0.1:3425/v1 becomes http://127.0.0.1:7788/gateway/local-3425/v1. keepmum scans each
request, then forwards the path as it came, so whatever API the gateway speaks (Messages,
Responses, Gemini) keeps working, and anything keepmum does not scan is refused.

Only that agent changes: another agent that goes to its provider directly keeps doing so, and its
key never reaches the gateway. The route holds the gateway's URL, so `keepmum protect off` points
the agent back where it was.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .config import LOOPBACK, gateway_problem
from .protect import keepmum_url

ROUTE = "/gateway/"
_GEMINI = re.compile(r"/v1[a-z0-9]*/(?:tunedModels|models)/[^/:]+:([A-Za-z]+)$")


def origin(url: str) -> str:
    """scheme://host[:port] of a URL: one gateway serves every API under it."""
    u = urlsplit(url)
    host = f"[{u.hostname}]" if ":" in (u.hostname or "") else u.hostname
    return f"{u.scheme}://{host}" + (f":{u.port}" if u.port else "")


def name_for(url: str, gateways: dict[str, str]) -> str:
    """A route name for a new gateway: local-3425 on this Mac, the host's name elsewhere."""
    u = urlsplit(url)
    if u.hostname in LOOPBACK:
        base = f"local-{u.port or (443 if u.scheme == 'https' else 80)}"
    else:
        labels = [x for x in (u.hostname or "gateway").lower().split(".") if x not in ("api", "www", "gateway")]
        base = re.sub(r"[^a-z0-9-]", "-", labels[0] if len(labels) > 1 else (labels or ["gateway"])[0])[:30].strip("-")
        base = base or "gateway"
    name, n = base, 2
    while name in gateways and gateways[name] != origin(url):
        name, n = f"{base}-{n}", n + 1
    return name


def find(url: str, gateways: dict[str, str]) -> tuple[str, str] | None:
    """The gateway `url` is under, and the path after it: (name, "/v1")."""
    url = url.rstrip("/")
    for name, base in sorted(gateways.items(), key=lambda kv: -len(kv[1])):
        if url == base or url.startswith(base + "/"):
            return name, url[len(base):]
    return None


def route_url(port: int, name: str, suffix: str = "") -> str:
    return f"{keepmum_url(port)}{ROUTE}{name}{suffix}"


def parse_route(value: str, port: int) -> tuple[str, str] | None:
    """(name, path after it) when `value` is one of keepmum's gateway routes."""
    if not isinstance(value, str):
        return None
    for host in ("127.0.0.1", "localhost"):
        prefix = f"http://{host}:{port}{ROUTE}"
        if value.startswith(prefix):
            m = re.fullmatch(r"([a-z0-9][a-z0-9-]*)(/.*)?", value[len(prefix):].rstrip("/"))
            if m:
                return m.group(1), m.group(2) or ""
    return None


def unroute(value: str, port: int, gateways: dict[str, str]) -> str | None:
    """The gateway URL a route stands for, or None (not a route, or its gateway is gone)."""
    r = parse_route(value, port)
    if r is None or r[0] not in gateways:
        return None
    return gateways[r[0]] + r[1]


def is_ours(value, port: int) -> bool:
    """keepmum on `port`: its root, or one of its gateway routes."""
    from .protect import is_keepmum_url

    return isinstance(value, str) and (is_keepmum_url(value, port) or parse_route(value, port) is not None)


class Adopter:
    """Puts keepmum in front of the gateways agents already use, during one `keepmum protect on`.
    `write(name, url)` saves a new gateway to the config file (audited by the caller)."""

    def __init__(self, port: int, gateways: dict[str, str], write):
        self.port = port
        self.gateways = dict(gateways)
        self.write = write
        self.adopted: dict[str, str] = {}  # agent -> the gateway URL it went to

    def __call__(self, url: str, who: str) -> str:
        """keepmum's route for `url`, the gateway `who` goes to now. Raises ProtectError if keepmum
        cannot forward there."""
        from .protect import ProtectError

        url = url.strip().rstrip("/")
        why = gateway_problem(url, self.port)
        if why:
            raise ProtectError(f"{who} goes to {url}, which keepmum cannot forward to: it {why}")
        hit = find(url, self.gateways)
        if hit is None:
            name = name_for(url, self.gateways)
            if name not in self.gateways:
                self.write(name, origin(url))
                self.gateways[name] = origin(url)
            hit = find(url, self.gateways)
        self.adopted[who] = url
        return route_url(self.port, *hit)


def api_of_route(rest: str) -> str | None:
    """The API keepmum scans at `rest`, the path after a gateway route, wherever the gateway puts
    it (OpenRouter's /api/v1/messages too); None: not a scanned endpoint, refused if it has a body."""
    if rest.endswith(("/v1/messages", "/v1/messages/count_tokens")):
        return "messages"
    if rest.endswith(("/responses", "/responses/compact")):
        return "responses"
    m = _GEMINI.search(rest)
    if m:
        return "gemini" if m.group(1) in ("generateContent", "streamGenerateContent") else "gemini-plain"
    return None
