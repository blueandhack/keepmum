"""The local proxy: scan requests before they leave, restore responses before the client sees them.

Fail closed everywhere. A request is forwarded only after it was scanned; if scanning fails,
the body is malformed or compressed in a way keepmum cannot read, or the endpoint is one keepmum
does not scan, the request goes nowhere and the client gets an API-style error. WebSocket
upgrades are refused (Codex then falls back to HTTPS). Logs carry paths, statuses and counts,
never bodies, values or headers.

Observe mode (`detect.observe`) is the exception, chosen by the user: every request is scanned
and counted, then forwarded exactly as it came, unmasked, and nothing is blocked.

One proxy serves every provider, chosen by path prefix:

    /v1/messages...                      Anthropic Messages API (Claude Code, pi)
    /openai/v1/responses...              OpenAI Responses API (Codex, pi or opencode with an API key)
    /chatgpt/backend-api/codex/responses OpenAI through a ChatGPT login (Codex, pi)

opencode keeps one OpenAI base URL for both logins, so an /openai/v1 request that carries a
ChatGPT account (`chatgpt-account-id`) goes to chatgpt.com/backend-api/codex instead: a ChatGPT
token is only good there. Scanned bodies bound for chatgpt.com are sent zstd-compressed, as
Codex sends them; that backend refuses large uncompressed ones.
"""

from __future__ import annotations

import gzip
import hmac
import json
import logging
import threading
import time
import zlib
from collections import Counter
from dataclasses import dataclass

import anyio
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .redact import Redactor
from .responses import ResponsesStreamRestorer, restore_responses
from .restore import DEFAULT_POLICY, StreamRestorer, restore_response, take_tools
from .stats import Stats, client_of, flows, provider_of

log = logging.getLogger("keepmum")

UPSTREAM = "https://api.anthropic.com"
OPENAI_UPSTREAM = "https://api.openai.com"
CHATGPT_UPSTREAM = "https://chatgpt.com"
MESSAGES = frozenset({"/v1/messages", "/v1/messages/count_tokens"})
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
# Not forwarded upstream; accept-encoding is replaced so responses arrive uncompressed, and
# x-keepmum-client (set by `keepmum protect` in client configs) is for keepmum's stats only.
HOP_HEADERS = frozenset({"host", "content-length", "connection", "keep-alive", "proxy-connection", "transfer-encoding",
                         "te", "trailer", "upgrade", "accept-encoding", "x-keepmum-client"})
RESPONSE_DROP = frozenset({"content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive"})


def found_by(findings: list[dict]) -> Counter:
    """Which detectors found the values in a request's new text: a built-in rule by its id, your
    own rule as user:<name>, or model, or known (masked before, found again by exact match)."""
    out = Counter()
    for f in findings:
        detector, rule = f["detector"], f["rule"]
        out[rule if detector == "rules" else f"user:{rule}" if detector == "user" else detector] += 1
    return out


def api_error(status: int, kind: str, message: str) -> JSONResponse:
    """An error in the Anthropic API's shape, so Claude Code shows it instead of crashing."""
    return JSONResponse({"type": "error", "error": {"type": kind, "message": f"keepmum: {message}"}}, status_code=status)


def _host_is_local(request: Request) -> bool:
    # Blocks DNS rebinding: a web page cannot reach the proxy through a name that resolves here.
    host = request.headers.get("host", "")
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    return name in LOCAL_HOSTS


def decode_body(raw: bytes, encoding: str) -> bytes:
    """The request body uncompressed. Codex and pi send zstd. Anything unknown raises ValueError."""
    encoding = encoding.strip().lower()
    if encoding in ("", "identity"):
        return raw
    if encoding == "zstd":
        import io
        import zstandard

        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw), read_across_frames=True) as reader:
            return reader.read()
    if encoding in ("gzip", "x-gzip"):
        return gzip.decompress(raw)
    if encoding == "deflate":
        return zlib.decompress(raw)
    raise ValueError(f"unsupported content-encoding {encoding!r}")


@dataclass(frozen=True)
class Target:
    """Where a request goes: the provider (for stats), its upstream, the path there, and the API
    whose body keepmum scans (None: not a scanned endpoint)."""
    provider: str
    base: str
    path: str
    api: str | None


def api_of(path: str) -> str | None:
    if path in MESSAGES:
        return "messages"
    if path.endswith("/responses") or path.endswith("/responses/compact"):
        return "responses"
    return None


class Proxy:
    def __init__(self, redactor: Redactor, upstream: str = UPSTREAM, policy: dict[str, str] | None = None,
                 client: httpx.AsyncClient | None = None, stats: Stats | None = None, stats_token: str | None = None,
                 info: dict | None = None, observe=None, openai_upstream: str = OPENAI_UPSTREAM,
                 chatgpt_upstream: str = CHATGPT_UPSTREAM, previews=True, audit=None):
        self.redactor = redactor
        self.upstream = upstream.rstrip("/")
        self.prefixed = {"/openai": openai_upstream.rstrip("/"), "/chatgpt": chatgpt_upstream.rstrip("/")}
        self.policy = policy or DEFAULT_POLICY
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10))
        self.stats = stats or Stats()
        self.audit = audit  # an audit.AuditLog: every request, without values (None: not audited)
        self.stats_token = stats_token
        self.info = info or {}  # shown in the stats feed: mode, pii, detector, version
        # A callable, re-checked per request (config.ObserveFlag), or a fixed bool.
        self._observe = observe if callable(observe) else (lambda: bool(observe))
        # The Logs page's first and last characters ([log] previews): a callable or a fixed bool.
        self._previews = previews if callable(previews) else (lambda: bool(previews))
        # The detectors and the vault are not thread-safe; scans run one at a time off the event loop.
        self._scan_lock = threading.Lock()
        self.app = Starlette(routes=[Route("/{path:path}", self.handle, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])])

    def target(self, path: str, chatgpt_account: bool = False) -> Target:
        if chatgpt_account and path.startswith("/openai/v1/"):
            rest = "/backend-api/codex" + path[len("/openai/v1"):]
            return Target("openai", self.prefixed["/chatgpt"], rest, api_of(rest))
        for prefix, base in self.prefixed.items():
            if path.startswith(prefix + "/"):
                rest = path[len(prefix):]
                return Target("openai", base, rest, api_of(rest))
        return Target(provider_of(self.upstream), self.upstream, path, "messages" if path in MESSAGES else None)

    # --- Entry point --------------------------------------------------------------

    async def handle(self, request: Request) -> Response:
        path = request.url.path
        if not _host_is_local(request):
            return api_error(403, "permission_error", "requests must come from this machine")
        if path.startswith("/_keepmum/"):
            return self._local(request, path)
        if request.headers.get("upgrade"):
            # Without a WebSocket library uvicorn hands an upgrade over as a plain GET: refuse it
            # here too, so it is never forwarded. Clients fall back to HTTPS.
            return api_error(403, "permission_error", "keepmum does not scan WebSockets; use HTTPS")
        t = self.target(path, "chatgpt-account-id" in request.headers)
        if t.api and request.method == "POST":
            return await self._scanned(request, t)
        body = await request.body()
        if self._observe():
            return await self._forward_raw(request, t, body, outcome="observed")
        if body or request.method not in ("GET", "HEAD", "OPTIONS"):
            # An endpoint keepmum does not scan must not carry content out unscanned.
            log.warning("blocked %s %s: endpoint is not scanned", request.method, path)
            self._record(path, "blocked", 403, origin=self._origin(request, t))
            return api_error(403, "permission_error", f"refusing to forward {request.method} {path}: keepmum does not scan this endpoint")
        return await self._forward_raw(request, t)

    def _local(self, request: Request, path: str) -> Response:
        if path == "/_keepmum/health":
            return JSONResponse({"ok": True})
        if path in ("/_keepmum/stats", "/_keepmum/history", "/_keepmum/flows", "/_keepmum/findings") and request.method == "GET":
            given = request.headers.get("authorization", "").removeprefix("Bearer ")
            if not self.stats_token or not hmac.compare_digest(given, self.stats_token):
                return api_error(401, "authentication_error", "missing or wrong stats token")
        if path == "/_keepmum/flows" and request.method == "GET":
            try:
                window = max(60.0, min(float(request.query_params.get("window", 3600)), 30 * 86400))
            except ValueError:
                return api_error(400, "invalid_request_error", "window must be a number of seconds")
            history = self.stats.history
            events = history.events() if history else list(self.stats.events)
            return JSONResponse({"window": window, **flows(events, time.time() - window)})
        if path == "/_keepmum/findings" and request.method == "GET":
            # What was masked in new text, newest first: types, rules, lengths, and the first and
            # last character of values of 8 or more ([log] previews). In memory only.
            if not self._previews():
                self.stats.drop_previews()
            return JSONResponse({"findings": self.stats.findings_page(), "previews": self._previews()})
        if path == "/_keepmum/history" and request.method == "GET":
            q = request.query_params
            try:
                before = float(q["before"]) if "before" in q else None
                limit = max(1, min(int(q.get("limit", 100)), 500))
            except ValueError:
                return api_error(400, "invalid_request_error", "before and limit must be numbers")
            history = self.stats.history
            events = history.page(before, limit, q.get("kind")) if history else []
            return JSONResponse({"events": events})
        if path == "/_keepmum/stats" and request.method == "GET":
            detector = self.redactor.detector
            detection = detector.report() if hasattr(detector, "report") else dict(getattr(detector, "funnel", {}))
            # The detector and pii setting can change while running: report the current ones.
            info = {**self.info, "detector": getattr(detector, "name", self.info.get("detector")),
                    "pii": getattr(detector, "pii", self.info.get("pii"))}
            return JSONResponse(self.stats.snapshot(upstream=self.upstream, vault_size=len(self.redactor.vault),
                                                    detection=detection, **info, observe=self._observe()))
        return api_error(404, "not_found_error", "no such keepmum endpoint")

    # --- Scanned endpoints ----------------------------------------------------------

    async def _scanned(self, request: Request, t: Target) -> Response:
        path, origin = request.url.path, self._origin(request, t)
        raw = await request.body()
        if self._observe():
            return await self._observed(request, t, raw)
        try:
            body = json.loads(decode_body(raw, request.headers.get("content-encoding", "")))
            if not isinstance(body, dict):
                raise ValueError("not an object")
        except Exception:
            self._record(path, "rejected", 400, origin=origin)
            return api_error(400, "invalid_request_error", "request body is not a JSON object keepmum can read; nothing was sent")
        started = time.perf_counter()
        try:
            masked, counts, new, found = await anyio.to_thread.run_sync(self._redact, body, t.api, origin)
        except Exception as e:  # any detector or redaction failure: block, never send raw
            log.error("blocked %s: scan failed (%s)", path, type(e).__name__)
            self._record(path, "blocked", 500, origin=origin)
            return api_error(500, "api_error", "could not scan this request, so it was not sent")
        scan_ms = (time.perf_counter() - started) * 1000
        payload = json.dumps(masked, ensure_ascii=False).encode("utf-8", "replace")
        url = self._url(request, t)
        headers = self._headers(request)
        if t.base == self.prefixed["/chatgpt"]:
            import zstandard

            payload = zstandard.ZstdCompressor().compress(payload)
            headers.append(("content-encoding", "zstd"))
        try:
            if masked.get("stream") and t.path not in ("/v1/messages/count_tokens",):
                return await self._stream(url, headers, payload, path, t, scan_ms, counts, new, origin=origin, found=found)
            resp = await self.client.post(url, headers=headers, content=payload)
        except httpx.HTTPError as e:
            log.error("upstream error on %s: %s", path, type(e).__name__)
            self._record(path, "upstream_error", 502, scan_ms, counts, new=new, origin=origin, found=found)
            return api_error(502, "api_error", f"upstream request failed ({type(e).__name__})")
        restored = Counter()
        content = resp.content
        if t.path != "/v1/messages/count_tokens" and resp.status_code < 300:
            try:
                data = json.loads(content)
                content = json.dumps(await anyio.to_thread.run_sync(self._restore, data, restored, t.api),
                                     ensure_ascii=False).encode("utf-8", "replace")
            except ValueError:
                pass  # not JSON: pass it through as it came
        self._warn_unresolved(path, restored)
        self._record(path, "forwarded", resp.status_code, scan_ms, counts, restored, new=new, origin=origin, found=found)
        log.info("%s -> %d redacted=%s restored=%s scan=%.0fms", path, resp.status_code, dict(counts), dict(restored), scan_ms)
        return Response(content, status_code=resp.status_code, headers=self._response_headers(resp))

    async def _observed(self, request: Request, t: Target, raw: bytes) -> Response:
        """Observe mode: count what masking would have done, then send the request unchanged."""
        path, origin = request.url.path, self._origin(request, t)
        counts, new, scan_ms, body, found = Counter(), 0, None, None, None
        try:
            body = json.loads(decode_body(raw, request.headers.get("content-encoding", "")))
            if isinstance(body, dict):
                started = time.perf_counter()
                _, counts, new, found = await anyio.to_thread.run_sync(self._redact, body, t.api, origin, "observed")
                scan_ms = (time.perf_counter() - started) * 1000
        except Exception as e:  # observing never stops a request
            log.warning("observe %s: not scanned (%s)", path, type(e).__name__)
        url = self._url(request, t)
        headers = self._headers(request, keep_encoding=True)
        try:
            if t.path != "/v1/messages/count_tokens" and isinstance(body, dict) and body.get("stream"):
                return await self._stream(url, headers, raw, path, t, scan_ms, counts, new, outcome="observed", origin=origin,
                                          found=found)
            resp = await self.client.post(url, headers=headers, content=raw)
        except httpx.HTTPError as e:
            self._record(path, "upstream_error", 502, scan_ms, counts, new=new, origin=origin, found=found)
            return api_error(502, "api_error", f"upstream request failed ({type(e).__name__})")
        self._record(path, "observed", resp.status_code, scan_ms, counts, new=new, origin=origin, found=found)
        log.info("%s -> %d (observe: sent unmasked) would mask=%s", path, resp.status_code, dict(counts))
        return Response(resp.content, status_code=resp.status_code, headers=self._response_headers(resp))

    @staticmethod
    def _origin(request: Request, t: Target) -> tuple[str, str]:
        """(client, provider) for the stats: who sent the request, and where it goes."""
        named = "".join(c for c in request.headers.get("x-keepmum-client", "").lower() if c.isalnum() or c in "-_.")[:32]
        return named or client_of(request.headers.get("user-agent", "")), t.provider

    def _record(self, path: str, outcome: str, status: int, scan_ms: float | None = None, counts: Counter | None = None,
                restored: Counter | None = None, new: int = 0, origin: tuple[str, str] | None = None,
                found: Counter | None = None) -> None:
        """Count a request for the stats feed, and add it to the audit log. Bodyless checks that
        carried no content (a GET of /v1/models) are counted but not audited."""
        tools = take_tools(restored) if restored else []
        self.stats.record(path, outcome, status, scan_ms, counts, restored, new=new, origin=origin)
        if self.audit is None or (scan_ms is None and outcome in ("forwarded", "observed")):
            return
        from .restore import UNRESOLVED

        client, provider = origin or (None, None)
        restored = dict(restored or {})
        unresolved = restored.pop(UNRESOLVED, 0)
        self.audit.append("request", client=client, provider=provider, path=path, outcome=outcome, status=status,
                          masked=dict(counts or {}), new=new, found=dict(found or {}) or None,
                          restored=restored or None, tools=tools or None, unresolved=unresolved or None)

    @staticmethod
    def _warn_unresolved(path: str, restored: Counter) -> None:
        if restored.get("unresolved"):
            log.warning("%s: %d placeholder(s) in the reply could not be restored (made by another keepmum process, "
                        "or before a restart); they reach the client as placeholders", path, restored["unresolved"])

    def _redact(self, body: dict, api: str = "messages", origin=None, outcome: str = "masked") -> tuple[dict, Counter, int, Counter]:
        """The masked body, spans masked per type, how many of them were in text this request
        brought (the rest are earlier turns, which clients resend every time), and which detectors
        found those. What the new text held goes to the Logs page, in memory only."""
        with self._scan_lock:
            self.redactor.counts.clear()
            self.redactor.fresh = 0
            self.redactor.findings = []
            self.redactor.previews = self._previews()
            if api == "responses":
                masked = self.redactor.redact_responses_request(body)
            else:
                masked = self.redactor.redact_request(body)
            self.stats.record_findings(self.redactor.findings, outcome, origin)
            return masked, Counter(self.redactor.counts), self.redactor.fresh, found_by(self.redactor.findings)

    def _restore(self, data: dict, counts: Counter, api: str = "messages") -> dict:
        with self._scan_lock:
            if api == "responses":
                return restore_responses(data, self.redactor.vault, self.policy, counts)
            return restore_response(data, self.redactor.vault, self.policy, counts)

    async def _stream(self, url, headers, payload, path, t: Target, scan_ms, counts, new=0, outcome="forwarded",
                      origin=None, found=None) -> Response:
        req = self.client.build_request("POST", url, headers=headers, content=payload)
        resp = await self.client.send(req, stream=True)
        restored = Counter()
        # A requested stream that succeeds is SSE; the ChatGPT backend sends it without a content type.
        is_sse = resp.status_code < 300
        kind = ResponsesStreamRestorer if t.api == "responses" else StreamRestorer
        # Observed requests went out unmasked: there is nothing to restore.
        restorer = kind(self.redactor.vault, self.policy, restored) if is_sse and outcome != "observed" else None

        async def body():
            try:
                async for chunk in resp.aiter_raw():
                    out = restorer.feed(chunk) if restorer else chunk
                    if out:
                        yield out
                if restorer:
                    tail = restorer.close()
                    if tail:
                        yield tail
            finally:
                await resp.aclose()
                self._warn_unresolved(path, restored)
                self._record(path, outcome, resp.status_code, scan_ms, counts, restored, new=new, origin=origin, found=found)
                log.info("%s -> %d (stream) redacted=%s restored=%s scan=%.0fms", path, resp.status_code,
                         dict(counts), dict(restored), scan_ms)

        return StreamingResponse(body(), status_code=resp.status_code, headers=self._response_headers(resp))

    # --- Plumbing -------------------------------------------------------------------

    @staticmethod
    def _url(request: Request, t: Target) -> str:
        return t.base + t.path + (f"?{request.url.query}" if request.url.query else "")

    async def _forward_raw(self, request: Request, t: Target, body: bytes = b"", outcome: str = "forwarded") -> Response:
        path = request.url.path
        try:
            resp = await self.client.request(request.method, self._url(request, t),
                                             headers=self._headers(request, keep_encoding=True), content=body or None)
        except httpx.HTTPError as e:
            return api_error(502, "api_error", f"upstream request failed ({type(e).__name__})")
        self._record(path, outcome, resp.status_code, origin=self._origin(request, t))
        log.info("%s %s -> %d (not scanned: no body)", request.method, path, resp.status_code)
        return Response(resp.content, status_code=resp.status_code, headers=self._response_headers(resp))

    @staticmethod
    def _headers(request: Request, keep_encoding: bool = False) -> list[tuple[str, str]]:
        # Auth headers (x-api-key, authorization) pass through unchanged and are never logged.
        # A scanned body is re-encoded (plain JSON, or zstd for chatgpt.com), so the client's
        # content-encoding no longer applies.
        drop = HOP_HEADERS if keep_encoding else HOP_HEADERS | {"content-encoding"}
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in drop]
        headers.append(("accept-encoding", "identity"))
        return headers

    @staticmethod
    def _response_headers(resp: httpx.Response) -> dict[str, str]:
        return {k: v for k, v in resp.headers.items() if k.lower() not in RESPONSE_DROP}
