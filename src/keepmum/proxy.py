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
    /gemini/v1beta/models/<m>:generateContent...   the Gemini API, with an API key (Gemini CLI)
    /codeassist/v1internal:generateContent...      Code Assist, "Sign in with Google" (Gemini CLI)
    /antigravity/v1internal:streamGenerateContent  Code Assist for the Antigravity CLI (agy), signed in with
                                                   Google: its own host, daily-cloudcode-pa

Code Assist also takes setup and metrics calls (`CODE_ASSIST_METADATA`): their bodies, read in
Gemini CLI 0.66, hold project ids, client metadata, counts and statuses, never conversation
text, so they pass through unscanned; so do Antigravity's own (`ANTIGRAVITY_METADATA`, read in
agy 1.5.3: a project id or a trajectory id). Any other Code Assist method is blocked. Vertex AI is not
scanned: `keepmum gemini` points it at /vertex, which refuses every request.

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
import re
import secrets
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

from .gateway import ROUTE, api_of_route
from .gemini import GeminiStreamRestorer, restore_gemini
from .local import CHAT_PATH as LOCAL_CHAT_PATH
from .local import APIS_LOCAL
from .local import PATHS as LOCAL_PATHS
from .handback import WRITTEN, Handbacks
from .handback import harm_messages, harm_pieces, harm_wrap, harm_written, nothing_left, only_removed
from .handback import _key as handback_key
from .handback import edited as handback_edited
from .handback import messages as handback_messages
from .handback import notice as handback_notice
from .handback import wrap as handback_wrap
from .handback import written as handback_written
from .review import MAX_WAIT, Review, Reviews
from .review import applies as review_applies
from .local import DEFAULT_MAX_TOKENS as LOCAL_MAX_TOKENS
from .local import NO_THINKING
from .local import keep_alive, thinking_fields, without_thinking
from .local import ChatStream, answer_text, from_chat, skills_text, to_chat
from .local import LocalRoute
from .local import prepare as prepare_local
from .redact import Redactor
from .responses import ResponsesStreamRestorer, restore_responses
from .restore import DEFAULT_POLICY, StreamRestorer, ToolPolicy, restore_response, rule_for, take_tools, take_urls
from .stats import Stats, client_of, flows, guard_summary, provider_of
from .tools import BULK, CONTACTS, definitions

log = logging.getLogger("keepmum")

UPSTREAM = "https://api.anthropic.com"
OPENAI_UPSTREAM = "https://api.openai.com"
CHATGPT_UPSTREAM = "https://chatgpt.com"
GEMINI_UPSTREAM = "https://generativelanguage.googleapis.com"
CODE_ASSIST_UPSTREAM = "https://cloudcode-pa.googleapis.com"
ANTIGRAVITY_UPSTREAM = "https://daily-cloudcode-pa.googleapis.com"
# Gemini API methods keepmum scans. generateContent and streamGenerateContent get the placeholder
# hint ("gemini"); countTokens and embeddings take no system instruction ("gemini-plain").
_GEMINI_METHOD = re.compile(r"^/v1[a-z0-9]*/(?:tunedModels|models)/[^/:]+:(generateContent|streamGenerateContent"
                            r"|countTokens|embedContent|batchEmbedContents)$")
_CODE_ASSIST_METHOD = re.compile(r"^/v1[a-z0-9]*:([A-Za-z]+)$")
# Code Assist setup and metrics: no conversation text, forwarded as they are (see above).
CODE_ASSIST_METADATA = frozenset({"loadCodeAssist", "onboardUser", "listExperiments", "retrieveUserQuota",
                                  "fetchAdminControls", "setCodeAssistGlobalUserSetting", "recordCodeAssistMetrics"})
ANTIGRAVITY_METADATA = CODE_ASSIST_METADATA | {"fetchUserInfo", "fetchAvailableModels", "retrieveUserQuotaSummary",
                                               "writeTrajectoryAcls"}
GOOGLE_PREFIXES = ("/gemini", "/codeassist", "/antigravity")
MESSAGES = frozenset({"/v1/messages", "/v1/messages/count_tokens"})
def _reviewed(decision, wait: int) -> str:
    """How a review ended, for the Logs page."""
    if decision.timed_out:
        return f"no answer in {wait} s: " + ("sent as written" if decision.action == "send" else "kept local")
    return {"send": "approved", "edit": "edited", "withhold": "withheld", "local": "kept local"}[decision.action]


# Of a streamed local answer, what is kept to check before it is handed back (handback.py "answer").
MAX_ANSWER_BYTES = 4 * 1024 * 1024
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


def api_of(path: str, prefix: str = "") -> str | None:
    """The API whose body keepmum scans at `path` behind `prefix`, "metadata" for Code Assist
    calls that pass through, or None: not an endpoint keepmum knows."""
    if prefix == "/gemini":
        m = _GEMINI_METHOD.match(path)
        if m is None:
            return None
        return "gemini" if m.group(1) in ("generateContent", "streamGenerateContent") else "gemini-plain"
    if prefix in ("/codeassist", "/antigravity"):
        m = _CODE_ASSIST_METHOD.match(path)
        method = m.group(1) if m else ""
        if method in ("generateContent", "streamGenerateContent"):
            return "gemini"
        if method == "countTokens":
            return "gemini-plain"
        metadata = ANTIGRAVITY_METADATA if prefix == "/antigravity" else CODE_ASSIST_METADATA
        return "metadata" if method in metadata else None
    if path in MESSAGES:
        return "messages"
    if path.endswith("/responses") or path.endswith("/responses/compact"):
        return "responses"
    return None


def wants_stream(t: Target, body, query: str) -> bool:
    """A streamed reply: `stream: true` for Anthropic and OpenAI, streamGenerateContent with
    alt=sse for Gemini (without it the stream comes back as one JSON array)."""
    if t.api in ("gemini", "gemini-plain"):
        return t.path.endswith(":streamGenerateContent") and "alt=sse" in query
    return isinstance(body, dict) and bool(body.get("stream")) and t.path != "/v1/messages/count_tokens"


def _error_text(raw: bytes, limit: int = 160) -> str:
    """A local server's error, short: its message if it sent one in JSON, else the start of the body."""
    try:
        data = json.loads(raw)
        err = data.get("error", data) if isinstance(data, dict) else data
        text = err.get("message") if isinstance(err, dict) else err
        text = text if isinstance(text, str) else json.dumps(err)
    except ValueError:
        text = raw.decode("utf-8", "replace")
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


class Proxy:
    def __init__(self, redactor: Redactor, upstream: str = UPSTREAM, policy: dict[str, str] | None = None,
                 client: httpx.AsyncClient | None = None, stats: Stats | None = None, stats_token: str | None = None,
                 info: dict | None = None, observe=None, openai_upstream: str = OPENAI_UPSTREAM,
                 chatgpt_upstream: str = CHATGPT_UPSTREAM, previews=True, context=True, audit=None,
                 gemini_upstream: str = GEMINI_UPSTREAM, code_assist_upstream: str = CODE_ASSIST_UPSTREAM,
                 tools=None, guards=None, commands=None, local=None, antigravity_upstream: str = ANTIGRAVITY_UPSTREAM,
                 local_skills=None, handback=None, review=None, speed=None, handback_harm=None):
        self.redactor = redactor
        # The checks on a request's texts (guard.Guard): "injection" on tool results, "harm" on what
        # the user typed. Warn-mode checks run after the request is sent, in `_background`.
        self.guards = [guards] if hasattr(guards, "check") else list(guards or [])
        # The check on the replies' shell commands (commands.CommandsCheck), or None.
        self.commands = commands
        # Where local mode sends a flagged request: a callable giving a check's local.Server list, in
        # the order tried, or None (local.py).
        self.local = local
        # The user's skills for a check's local turns: a callable giving its local.Skill list, or None.
        self.local_skills = local_skills
        # What follows a local turn of the injection check: a callable giving ([guard] local_handback,
        # local_handback_instructions), and the results handed back so far (handback.py).
        self._handback = handback if callable(handback) else (lambda: ("stay", ""))
        self.handbacks = Handbacks()
        # The same for the harm check ([guard] local_handback_harm): "stay", or "rewrite" what was typed.
        self._handback_harm = handback_harm if callable(handback_harm) else (lambda: "stay")
        self.harm_handbacks = Handbacks()
        # Whether the user reviews a summary or rewrite before it goes: a callable giving ([guard]
        # local_handback_review, _wait, _timeout), and the reviews waiting for an answer (review.py).
        self._review = review if callable(review) else (lambda: ("off", 60, "stay"))
        self.reviews = Reviews()
        # How fast local turns go: a callable giving ([guard] local_keep_loaded, local_answer_thinking).
        self._speed = speed if callable(speed) else (lambda: ("5m", True))
        # Whether each local server is Ollama, asked once: Ollama gets keep_alive after a request.
        self._ollama: dict[str, bool] = {}
        # "answer" under review: the flagged results whose hand-back waits for the user, by key; a
        # request carrying one waits for the answer (`_wait_for_reviews`).
        self._held: dict[bytes, anyio.Event] = {}
        # (server URL, API) pairs a server answered 404/405/501 for and then answered as Chat
        # Completions: asked that way from then on, until keepmum restarts.
        self._chat_only: set[tuple[str, str]] = set()
        self._background: set = set()
        self.upstream = upstream.rstrip("/")
        self.prefixed = {"/openai": openai_upstream.rstrip("/"), "/chatgpt": chatgpt_upstream.rstrip("/"),
                         "/gemini": gemini_upstream.rstrip("/"), "/codeassist": code_assist_upstream.rstrip("/"),
                         "/antigravity": antigravity_upstream.rstrip("/")}
        # Other gateways keepmum forwards to (config [gateways], gateway.py): name -> URL.
        self.gateways: dict[str, str] = {}
        # The servers seen in agents' tool lists (tools.ToolRegistry); None: no fingerprints, no counts.
        self.tools = tools
        self._destinations: dict[str, list[str]] = {}  # config [destinations] (destinations.py)
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
        # The text around each value on the Logs page, values shown as types ([log] context).
        self._context = context if callable(context) else (lambda: bool(context))
        # The detectors and the vault are not thread-safe; scans run one at a time off the event loop.
        self._scan_lock = threading.Lock()
        self.app = Starlette(routes=[Route("/{path:path}", self.handle, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])])

    @property
    def policy(self) -> ToolPolicy:
        return self._policy

    @policy.setter
    def policy(self, policy: dict) -> None:
        self._policy = ToolPolicy(policy, self.tools, self._destinations)

    @property
    def destinations(self) -> dict[str, list[str]]:
        return self._destinations

    @destinations.setter
    def destinations(self, hosts: dict[str, list[str]]) -> None:
        self._destinations = dict(hosts)
        self._policy = ToolPolicy(self._policy, self.tools, self._destinations)

    def target(self, path: str, chatgpt_account: bool = False) -> Target:
        if path.startswith(ROUTE):
            name, _, rest = path[len(ROUTE):].partition("/")
            base = self.gateways.get(name, "")
            # An unknown gateway has no base: never forwarded (handle refuses it).
            return Target(provider_of(base) if base else "unknown", base, "/" + rest, api_of_route("/" + rest))
        if chatgpt_account and path.startswith("/openai/v1/"):
            rest = "/backend-api/codex" + path[len("/openai/v1"):]
            return Target("openai", self.prefixed["/chatgpt"], rest, api_of(rest))
        for prefix, base in self.prefixed.items():
            if path.startswith(prefix + "/"):
                rest = path[len(prefix):]
                provider = "google" if prefix in GOOGLE_PREFIXES else "openai"
                return Target(provider, base, rest, api_of(rest, prefix))
        return Target(provider_of(self.upstream), self.upstream, path, "messages" if path in MESSAGES else None)

    # --- Entry point --------------------------------------------------------------

    async def handle(self, request: Request) -> Response:
        path = request.url.path
        if not _host_is_local(request):
            return api_error(403, "permission_error", "requests must come from this machine")
        if path == "/_keepmum/reviews" or path.startswith("/_keepmum/reviews/"):
            return await self._reviews_route(request, path)
        if path.startswith("/_keepmum/"):
            return self._local(request, path)
        if request.headers.get("upgrade"):
            # Without a WebSocket library uvicorn hands an upgrade over as a plain GET: refuse it
            # here too, so it is never forwarded. Clients fall back to HTTPS.
            return api_error(403, "permission_error", "keepmum does not scan WebSockets; use HTTPS")
        if path.startswith("/vertex/"):
            self._record(path, "blocked", 403, origin=self._origin(request, Target("google", "", path, None)))
            return api_error(403, "permission_error", "keepmum does not scan Vertex AI, so nothing was sent; "
                             "sign in to Gemini CLI with an API key or with Google")
        if path.startswith(ROUTE):
            self._observe()  # re-reads the config file if it changed: `protect on` may have just added this gateway
        t = self.target(path, "chatgpt-account-id" in request.headers)
        if path.startswith(ROUTE) and not t.base:
            self._record(path, "blocked", 404, origin=self._origin(request, t))
            return api_error(404, "not_found_error", f"no gateway named {path[len(ROUTE):].split('/')[0]!r} "
                             "([gateways] in ~/.config/keepmum/config.toml), so nothing was sent")
        if t.api == "metadata" and request.method == "POST":
            return await self._forward_raw(request, t, await request.body(), note="Code Assist setup, no content")
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

    async def _reviews_route(self, request: Request, path: str) -> Response:
        """GET /_keepmum/reviews: the hand-backs waiting for the user (review.py), and the settings;
        asking says someone is there to answer. POST /_keepmum/reviews/<id> {"action": send | edit |
        withhold | local, "text": the edit}: the answer. The stats token, as for the stats feed."""
        given = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not self.stats_token or not hmac.compare_digest(given, self.stats_token):
            return api_error(401, "authentication_error", "missing or wrong stats token")
        if path == "/_keepmum/reviews" and request.method == "GET":
            self.reviews.seen()
            mode, wait, timeout = self._review()
            return JSONResponse({"reviews": self.reviews.pending(), "mode": mode, "wait": wait, "timeout": timeout})
        if request.method == "POST" and path.count("/") == 3:
            try:
                answer = json.loads(await request.body() or b"{}")
            except ValueError:
                return api_error(400, "invalid_request_error", "the body must be JSON: {\"action\": …}")
            why = self.reviews.decide(path.rsplit("/", 1)[-1], str(answer.get("action")), answer.get("text"))
            if why:
                return api_error(404 if why.startswith("no such") else 400, "invalid_request_error", why)
            return JSONResponse({"ok": True})
        return api_error(405, "invalid_request_error", "GET /_keepmum/reviews, or POST /_keepmum/reviews/<id>")

    def _local(self, request: Request, path: str) -> Response:
        if path == "/_keepmum/health":
            return JSONResponse({"ok": True})
        if path in ("/_keepmum/stats", "/_keepmum/history", "/_keepmum/flows", "/_keepmum/findings",
                    "/_keepmum/tools", "/_keepmum/live") and request.method == "GET":
            given = request.headers.get("authorization", "").removeprefix("Bearer ")
            if not self.stats_token or not hmac.compare_digest(given, self.stats_token):
                return api_error(401, "authentication_error", "missing or wrong stats token")
        if path == "/_keepmum/live" and request.method == "GET":
            # The dashboard's fast poll while its Live flow is on screen: requests out at a provider
            # now (client, provider, kind) and the events since `after`. Small, so it can poll often.
            try:
                after = float(request.query_params.get("after", 0))
            except ValueError:
                return api_error(400, "invalid_request_error", "after must be a number")
            return JSONResponse(self.stats.live(after))
        if path == "/_keepmum/flows" and request.method == "GET":
            try:
                window = max(60.0, min(float(request.query_params.get("window", 3600)), 30 * 86400))
            except ValueError:
                return api_error(400, "invalid_request_error", "window must be a number of seconds")
            history = self.stats.history
            events = history.events() if history else list(self.stats.events)
            return JSONResponse({"window": window, **flows(events, time.time() - window)})
        if path == "/_keepmum/findings" and request.method == "GET":
            # What was masked in new text, newest first: types, rules, lengths, the first and last
            # character of values of 8 or more ([log] previews), and the text around each value
            # with every masked value in it shown as its type ([log] context). In memory only.
            if not self._previews():
                self.stats.drop_previews()
            if not self._context():
                self.stats.drop_context()
            return JSONResponse({"findings": self.stats.findings_page(), "previews": self._previews(),
                                 "context": self._context(), "checks": self.stats.check_texts_page()})
        if path == "/_keepmum/tools" and request.method == "GET":
            # The network tools agents offered, each with its restore setting: names, never values.
            servers = self.tools.snapshot(self.policy) if self.tools else []
            return JSONResponse({"servers": servers, "contacts": list(CONTACTS), "bulk": BULK})
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
        # Results handed back to the cloud (handback.py) go as their replacements; a local model
        # still gets the request as the agent wrote it.
        seen: list = []  # the handed-back results this request carries
        if self._held:
            await self._wait_for_reviews(body, path)
        original, (body, _) = body, self._handed_back(body, seen)
        first = handed = None
        while True:
            try:
                masked, counts, new, found, texts = await anyio.to_thread.run_sync(self._redact, body, t.api, origin)
            except Exception as e:  # any detector or redaction failure: block, never send raw
                log.error("blocked %s: scan failed (%s)", path, type(e).__name__)
                self._record(path, "blocked", 500, origin=origin)
                return api_error(500, "api_error", "could not scan this request, so it was not sent")
            # keepmum's own replacements are not tool output: what the local model wrote in them was
            # checked on its own (`_hand_back`), and their wording reads like an injection to the model.
            texts["injection"] = [i for i in texts.get("injection") or [] if not self.handbacks.made(i[1])]
            texts["harm"] = [i for i in texts.get("harm") or [] if not self.harm_handbacks.made(i[1])]
            refused = await self._guarded(texts, path, origin, kind="masked" if new else "clean", api=t.api)
            if isinstance(refused, LocalRoute):
                if first is None and (handed := await self._hand_back(refused, texts, path)):
                    if handed.get("kept"):  # what the local model wrote was flagged: this turn stays local
                        self.stats.note_guard(refused.entries, handed_back=handed)
                        return await self._answer_locally(request, t, original, refused, origin, texts)
                    seen = []
                    first, (body, _) = refused, self._handed_back(original, seen)
                    continue  # scanned again: what the local model wrote is checked like any result
                if first is not None:
                    log.info("%s: the %s check flagged what the local model wrote too; answered locally", path, refused.check)
                    self.stats.note_guard(first.entries, handed_back={**handed, "kept": "what the local model wrote was flagged too"})
                return await self._answer_locally(request, t, original, refused, origin, texts)
            if refused is not None:
                return refused
            break
        if first is not None:
            self.stats.note_guard(first.entries, handed_back=handed)
        # The first request to carry a replacement to the cloud is its hand-back on the flow diagram.
        handed_back = next(iter(self.handbacks.first_sent(seen) + self.harm_handbacks.first_sent(seen)), None)
        scan_ms = (time.perf_counter() - started) * 1000
        payload = json.dumps(masked, ensure_ascii=False).encode("utf-8", "replace")
        url = self._url(request, t)
        headers = self._headers(request)
        if t.base == self.prefixed["/chatgpt"]:
            import zstandard

            payload = zstandard.ZstdCompressor().compress(payload)
            headers.append(("content-encoding", "zstd"))
        flight = self.stats.depart(origin, "masked" if new else "clean", handed_back=handed_back)
        try:
            if wants_stream(t, masked, request.url.query):
                return await self._stream(url, headers, payload, path, t, scan_ms, counts, new, origin=origin, found=found,
                                          texts=texts, flight=flight)
            resp = await self.client.post(url, headers=headers, content=payload)
        except httpx.HTTPError as e:
            log.error("upstream error on %s: %s", path, type(e).__name__)
            self._record(path, "upstream_error", 502, scan_ms, counts, new=new, origin=origin, found=found, flight=flight)
            return api_error(502, "api_error", f"upstream request failed ({type(e).__name__})")
        restored = Counter()
        content = resp.content
        if t.path != "/v1/messages/count_tokens" and resp.status_code < 300:
            try:
                data = json.loads(content)
                policy = self._reply_policy(path, origin, "masked" if new else "clean", texts)
                content = json.dumps(await anyio.to_thread.run_sync(self._restore, data, restored, t.api, policy),
                                     ensure_ascii=False).encode("utf-8", "replace")
            except ValueError:
                pass  # not JSON: pass it through as it came
        self._warn_unresolved(path, restored)
        self._record(path, "forwarded", resp.status_code, scan_ms, counts, restored, new=new, origin=origin, found=found,
                     flight=flight)
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
                _, counts, new, found, texts = await anyio.to_thread.run_sync(self._redact, body, t.api, origin, "observed")
                scan_ms = (time.perf_counter() - started) * 1000
                await self._guarded(texts, path, origin, observing=True, kind="observed")
        except Exception as e:  # observing never stops a request
            log.warning("observe %s: not scanned (%s)", path, type(e).__name__)
        url = self._url(request, t)
        headers = self._headers(request, keep_encoding=True)
        # Only a scanned body is drawn on the dashboard's flow; a bodyless or unreadable one is not.
        flight = self.stats.depart(origin, "observed") if scan_ms is not None else None
        try:
            if wants_stream(t, body, request.url.query):
                return await self._stream(url, headers, raw, path, t, scan_ms, counts, new, outcome="observed", origin=origin,
                                          found=found, flight=flight)
            resp = await self.client.post(url, headers=headers, content=raw)
        except httpx.HTTPError as e:
            self._record(path, "upstream_error", 502, scan_ms, counts, new=new, origin=origin, found=found, flight=flight)
            return api_error(502, "api_error", f"upstream request failed ({type(e).__name__})")
        self._record(path, "observed", resp.status_code, scan_ms, counts, new=new, origin=origin, found=found, flight=flight)
        log.info("%s -> %d (observe: sent unmasked) would mask=%s", path, resp.status_code, dict(counts))
        return Response(resp.content, status_code=resp.status_code, headers=self._response_headers(resp))

    @staticmethod
    def _origin(request: Request, t: Target) -> tuple[str, str]:
        """(client, provider) for the stats: who sent the request, and where it goes."""
        named = "".join(c for c in request.headers.get("x-keepmum-client", "").lower() if c.isalnum() or c in "-_.")[:32]
        return named or client_of(request.headers.get("user-agent", "")), t.provider

    def _record(self, path: str, outcome: str, status: int, scan_ms: float | None = None, counts: Counter | None = None,
                restored: Counter | None = None, new: int = 0, origin: tuple[str, str] | None = None,
                found: Counter | None = None, guard: list[dict] | None = None, flight: int | None = None) -> None:
        """Count a request for the stats feed, and add it to the audit log. Bodyless checks that
        carried no content (a GET of /v1/models) are counted but not audited."""
        tools = take_tools(restored) if restored else []
        in_urls = take_urls(restored) if restored else {}
        if in_urls:
            log.warning("%s: %d placeholder(s) inside web addresses in the reply kept: a client that shows the "
                        "reply could otherwise send the real value there", path, sum(in_urls.values()))
        self.stats.record(path, outcome, status, scan_ms, counts, restored, new=new, origin=origin, tools=tools, guard=guard,
                          flight=flight)
        if self.audit is None or (scan_ms is None and outcome in ("forwarded", "observed")):
            return
        from .restore import UNRESOLVED

        client, provider = origin or (None, None)
        restored = dict(restored or {})
        unresolved = restored.pop(UNRESOLVED, 0)
        self.audit.append("request", client=client, provider=provider, path=path, outcome=outcome, status=status,
                          masked=dict(counts or {}), new=new, found=dict(found or {}) or None,
                          restored=restored or None, tools=tools or None, unresolved=unresolved or None,
                          in_urls=in_urls or None)

    @staticmethod
    def _warn_unresolved(path: str, restored: Counter) -> None:
        if restored.get("unresolved"):
            log.warning("%s: %d placeholder(s) in the reply could not be restored (made by another keepmum process, "
                        "or before a restart); they reach the client as placeholders", path, restored["unresolved"])

    def _redact(self, body: dict, api: str = "messages", origin=None, outcome: str = "masked"):
        """The masked body, spans masked per type, how many of them were in text this request
        brought (the rest are earlier turns, which clients resend every time), which detectors
        found those, and the texts for the guards (tool results, what the user typed). What the new text held goes
        to the Logs page, in memory only."""
        with self._scan_lock:
            self.redactor.counts.clear()
            self.redactor.fresh = 0
            self.redactor.findings = []
            self.redactor.tool_results = []
            self.redactor.user_texts = []
            self.redactor.typed_in = {}
            self.redactor.shown = {}
            self.redactor.previews = self._previews()
            self.redactor.context = self._context()
            if api == "responses":
                masked = self.redactor.redact_responses_request(body)
            elif api in ("gemini", "gemini-plain"):
                masked = self.redactor.redact_gemini_request(body, hint=api == "gemini")
            else:
                masked = self.redactor.redact_request(body)
            self.stats.record_findings(self.redactor.findings, outcome, origin)
            result = (masked, Counter(self.redactor.counts), self.redactor.fresh, found_by(self.redactor.findings),
                      {"injection": self.redactor.tool_results, "harm": self.redactor.user_texts,
                       "shown": self.redactor.shown, "typed_in": self.redactor.typed_in})
            self.redactor.tool_results, self.redactor.user_texts, self.redactor.shown = [], [], {}
            self.redactor.typed_in = {}
        if self.tools is not None:
            try:
                self.tools.observe(definitions(body, api), origin[0] if origin else None,
                                   lambda name: rule_for(name, self.policy)[1] != "restore")
            except Exception as e:  # the registry never stops a request
                log.warning("tool list not recorded (%s)", type(e).__name__)
        return result

    async def _guarded(self, texts: dict, path: str, origin, observing: bool = False, kind: str = "clean",
                       api: str | None = None) -> Response | LocalRoute | None:
        """Run each guard on its texts: None to send the request, or the refusal. Block mode checks
        before the request goes; warn mode, and every mode while observing, checks after it is sent,
        so a warning adds no delay. `kind` is how the request goes out (masked, clean, observed): a
        warning carries it, with an id of the request, so the flow diagram moves the request to Flagged."""
        request = secrets.token_hex(6)
        for guard in self.guards:
            items = texts.get(guard.name) or []
            mode = guard.mode
            if mode == "off" or not items:
                continue
            if mode in ("block", "local") and not observing:
                refused = await self._check_now(guard, items, path, origin, api, texts)
                if refused is not None:
                    return refused
            else:
                self._later(self._check_later(guard, items, path, origin, kind, request, texts))
        return None

    async def _check_now(self, guard, items, path: str, origin, api: str | None = None,
                         texts: dict | None = None) -> Response | LocalRoute | None:
        """Block mode: a hit, or a text flagged before and still in the conversation, refuses the
        request. A guard that fails refuses it too: it cannot vouch for what it did not check.
        Local mode: such a request goes to the local model instead (local.py), if its API is one
        the local server speaks; otherwise it is refused, as in block mode."""
        try:
            hits = await anyio.to_thread.run_sync(guard.check, items, True)
        except Exception as e:
            log.error("blocked %s: %s check failed (%s)", path, guard.name, type(e).__name__)
            self._record(path, "blocked", 500, origin=origin)
            return api_error(500, "api_error", f"the {guard.name} check failed, so this request was not sent "
                             f"(`keepmum guard {guard.name} warn` sends such requests with a warning)")
        if not hits:
            return None
        if guard.mode == "local" and api in APIS_LOCAL:
            entries = self._record_hits(guard, hits, "local", path, origin, self._shown(hits, items, texts))
            return LocalRoute(guard.name, hits, entries)
        self._record_hits(guard, hits, "blocked", path, origin, self._shown(hits, items, texts))
        self._record(path, "blocked", 403, origin=origin, guard=guard_summary(hits, "blocked"))
        if guard.name == "harm":
            cats = sorted({c for h in hits for c in h.get("categories") or []})
            return api_error(403, "permission_error",
                             "the harm check flagged what you typed" + (f" ({', '.join(cats)})" if cats else "")
                             + ", so this request was not sent. Rephrase it (rewind, or /clear), or run "
                             "`keepmum guard harm warn` to send such requests with a warning.")
        sources = ", ".join(sorted({h["source"] or "a tool" for h in hits}))
        return api_error(403, "permission_error",
                         f"the injection check found a likely prompt injection in what {sources} returned, so this "
                         "request was not sent. Remove that result from the conversation (rewind, or /clear), or run "
                         "`keepmum guard injection warn` to send such requests with a warning.")

    def _handed_back(self, body: dict, seen: list | None = None) -> tuple[dict, int]:
        """The body with the results handed back so far replaced: while the injection check is in
        local mode and hands back. Turned off, or set to stay, the request goes as the agent wrote it.
        `seen` gets the replaced results' keys (Handbacks.first_sent)."""
        guard = next((g for g in self.guards if g.name == "injection"), None)
        n = 0
        if guard is not None and guard.mode == "local" and self._handback()[0] != "stay":
            body, n = self.handbacks.apply(body, seen)
        harm = next((g for g in self.guards if g.name == "harm"), None)
        if harm is not None and harm.mode == "local" and self._handback_harm() == "rewrite":
            body, m = self.harm_handbacks.apply(body, seen)
            n += m
        return body, n

    async def _hand_back(self, route: LocalRoute, texts: dict, path: str) -> dict | None:
        """[guard] local_handback (handback.py): a replacement for each result the injection check
        flagged, so the turn can go to the cloud. What was done, for the Logs page; None leaves
        the turn to the local model (stay, answer, or no replacement could be written)."""
        if route.check == "harm":
            return await self._hand_back_harm(route, texts, path)
        strategy, instructions = self._handback()
        if route.check != "injection" or strategy not in ("withhold", *WRITTEN):
            return None
        flagged = self._flagged_texts(route, texts)
        if not flagged or any(self.handbacks.kept(text) for text in flagged):
            return None  # what the local model wrote for it was flagged before: it stays local
        if strategy == "withhold":
            for text, source in flagged.items():
                self.handbacks.add(text, handback_notice(strategy, source), strategy)
            log.info("%s: the flagged result withheld; the request goes on to the cloud", path)
            return {"strategy": strategy}
        skills = self.local_skills("handback") if callable(self.local_skills) else []
        skills = await anyio.to_thread.run_sync(skills_text, skills) if skills else ""
        prompt = (texts.get("harm") or [(None, None, None)])[-1][1]
        done, server, reviews = [], None, []
        guard = next((g for g in self.guards if g.name == "injection"), None)
        for text, source in flagged.items():
            got = await self._write_locally(strategy, text, source, prompt, instructions, skills, path)
            if got is None:
                return None
            out, server = got
            try:  # what the local model wrote, on its own: keepmum's line around it is not checked
                hits = await anyio.to_thread.run_sync(guard.check, [("the local model", out, True)], True) if guard else []
            except Exception as e:
                log.warning("%s: the local %s was not checked (%s); answered locally", path, strategy, type(e).__name__)
                return None
            if hits:
                for t in flagged:
                    self.handbacks.keep_local(t)
                log.info("%s: the injection check flagged the local model's %s; answered locally", path, strategy)
                return {"strategy": strategy, "model": server.model, "url": server.url,
                        "kept": "what the local model wrote was flagged too"}
            mode, wait, timeout = self._review()
            if review_applies(mode, strategy) and self.reviews.present():
                log.info("%s: holding the %s for the user's review (up to %d s)", path, strategy, wait)
                decision = await self.reviews.ask(Review(strategy, source, path, text, out, server.model, wait), timeout)
                reviews.append(_reviewed(decision, wait))
                if decision.action == "local":
                    for t in flagged:
                        self.handbacks.keep_local(t)
                    log.info("%s: the %s was kept on this Mac (%s)", path, strategy, reviews[-1])
                    return {"strategy": strategy, "model": server.model, "url": server.url, "review": reviews[-1],
                            "kept": "you kept it on this Mac" if not decision.timed_out else f"no answer in {wait} s"}
                if decision.action == "withhold":
                    done.append((text, handback_notice("withhold", source)))
                    continue
                if decision.action == "edit":
                    out = decision.text
                    if await self._edit_flagged(guard, out):  # the user's own text: noted if flagged, theirs to send
                        reviews[-1] = "edited; the check flagged the edit, sent as you approved it"
            done.append((text, handback_wrap(strategy, source, out, custom=bool(instructions))))
        for text, replacement in done:
            self.handbacks.add(text, replacement, strategy)
        log.info("%s: the local model %s at %s wrote a %s of the flagged result; checking it", path, server.model,
                 server.url, strategy)
        return {"strategy": strategy, "model": server.model, "url": server.url, **({"review": "; ".join(reviews)} if reviews else {})}

    async def _hand_back_harm(self, route: LocalRoute, texts: dict, path: str) -> dict | None:
        """[guard] local_handback_harm "rewrite" (handback.py): what the user typed, with what the harm
        check flagged taken out by the local model, in place of what they typed, so the turn can go to
        the cloud. Removal only: what the local model wrote is checked by the harm check, and if it is
        still flagged, or nothing is left, the turn stays local. None leaves it to the local model."""
        if self._handback_harm() != "rewrite":
            return None
        flagged = self._flagged_texts(route, texts)
        whole = (texts or {}).get("typed_in") or {}
        if not flagged or any(self.harm_handbacks.kept(text) for text in flagged):
            return None
        guard = next((g for g in self.guards if g.name == "harm"), None)
        skills = self.local_skills("handback") if callable(self.local_skills) else []
        skills = await anyio.to_thread.run_sync(skills_text, skills) if skills else ""
        done, server = [], None
        for text in flagged:
            full = whole.get(text)
            if not full or text not in full:  # what was typed is not one piece of its message: kept local
                return None
            got = await self._write_locally("rewrite", text, None, None, "", skills, path, check="harm")
            if got is None:
                return None
            out, server = got
            out = harm_written(out)
            changed = not only_removed(text, out)
            try:
                hits = [] if changed or not guard else await anyio.to_thread.run_sync(
                    guard.check, [(None, piece, True) for piece in harm_pieces(out)], True)
            except Exception as e:
                log.warning("%s: the local rewrite was not checked (%s); answered locally", path, type(e).__name__)
                return None
            if changed or hits or nothing_left(out):
                for t in flagged:
                    self.harm_handbacks.keep_local(t)
                why = ("the rewrite changed what was typed, not only took a part out" if changed
                       else "the harm check flagged the rewrite too" if hits
                       else "nothing was left once the flagged part was out")
                log.info("%s: %s; answered locally", path, why)
                return {"strategy": "rewrite", "model": server.model, "url": server.url, "kept": why}
            mine = harm_wrap(out)
            done.append((full, full.replace(text, mine, 1), mine))
        for full, replacement, mine in done:
            self.harm_handbacks.add(full, replacement, "rewrite")
            self.harm_handbacks.mark_made(mine)
        log.info("%s: the local model %s at %s took the flagged part out of what was typed, and the harm check passed the rest", path,
                 server.model, server.url)
        return {"strategy": "rewrite", "model": server.model, "url": server.url}

    @staticmethod
    def _flagged_texts(route: LocalRoute, texts: dict | None) -> dict[str, str | None]:
        """The texts of a route's hits, each with its source (the tool)."""
        items = (texts or {}).get(route.check) or []
        return {items[h["at"]][1]: items[h["at"]][0] for h in route.hits
                if isinstance(h.get("at"), int) and 0 <= h["at"] < len(items)}

    async def _write_locally(self, strategy: str, text: str, source: str | None, prompt: str | None,
                             instructions: str, skills: str, path: str, check: str = "injection") -> tuple[str, object] | None:
        """A summary or rewrite of a flagged result (or, for the harm check, of what was typed) from the
        check's local servers, the first that writes one, in Chat Completions (every local server speaks
        it), with no tools."""
        servers = self.local(check) if callable(self.local) else []
        failures = []
        for server in servers:
            data = {"model": server.model, "stream": False, "max_tokens": LOCAL_MAX_TOKENS,
                    "messages": (harm_messages(text, skills) if check == "harm"
                                 else handback_messages(strategy, text, source, prompt, instructions, skills)), **NO_THINKING}
            try:
                resp = await self.client.post(server.url + LOCAL_CHAT_PATH, json=data)
                if resp.status_code in (400, 422):  # a server that refuses the fields that turn thinking off
                    resp = await self.client.post(server.url + LOCAL_CHAT_PATH,
                                                  json={k: v for k, v in data.items() if k not in NO_THINKING})
            except httpx.HTTPError as e:
                failures.append(f"{server.url} did not answer ({type(e).__name__})")
                continue
            if not 200 <= resp.status_code < 300:
                failures.append(f"{server.model} at {server.url}: HTTP {resp.status_code}")
                continue
            try:
                out = handback_written(resp.json())
            except (ValueError, AttributeError, TypeError):
                out = ""
            if out:
                self._later(self._keep_loaded(server))
                return out, server
            failures.append(f"{server.model} at {server.url}: no text")
        log.warning("%s: no local model wrote a %s of the flagged result (%s); answered locally", path, strategy,
                    "; ".join(failures) or f"no local server is set for the {check} check")
        return None

    async def _after_answer(self, route: LocalRoute, flagged: dict, answer: str, path: str, model: str = "") -> None:
        """local_handback "answer": once the local model has answered, the flagged results are withheld
        from the next request on, which goes to the cloud, unless the injection check flags the answer."""
        guard = next((g for g in self.guards if g.name == "injection"), None)
        if answer.strip() and guard is not None:
            try:
                hits = await anyio.to_thread.run_sync(guard.check, [("the local model", answer, True)], True)
            except Exception as e:  # unchecked: not handed back
                log.warning("%s: the local answer was not checked (%s); the conversation stays local", path, type(e).__name__)
                return
            if hits:
                log.info("%s: the injection check flagged the local answer; the conversation stays local", path)
                self.stats.note_guard(route.entries, handed_back={"strategy": "answer",
                                                                  "kept": "the local model's answer was flagged too"})
                return
        mode, wait, timeout = self._review()
        if review_applies(mode, "answer") and self.reviews.present():
            for text in flagged:  # held now, so the next request waits even if it comes at once
                self._held[handback_key(text)] = anyio.Event()
            log.info("%s: holding the hand-back after the local answer for the user's review (up to %d s)", path, wait)
            self._later(self._review_answer(route, flagged, answer, model, path, wait, timeout))
            return
        for text, source in flagged.items():
            self.handbacks.add(text, handback_notice("answer", source), "answer")
        self.stats.note_guard(route.entries, handed_back={"strategy": "answer"})

    async def _review_answer(self, route: LocalRoute, flagged: dict, answer: str, model: str, path: str,
                             wait: int, timeout: str) -> None:
        """The user's review of the hand-back after a local answer, one flagged result at a time;
        then the requests waiting on it go on."""
        reviews: list[str] = []
        guard = next((g for g in self.guards if g.name == "injection"), None)
        try:
            for text, source in flagged.items():
                notice = handback_notice("answer", source)
                decision = await self.reviews.ask(Review("answer", source, path, text, notice, model, wait, answer=answer),
                                                  timeout)
                reviews.append(_reviewed(decision, wait))
                if decision.action == "local":
                    for t in flagged:
                        self.handbacks.keep_local(t)
                    self.stats.note_guard(route.entries, handed_back={
                        "strategy": "answer", "review": "; ".join(reviews),
                        "kept": "you kept it on this Mac" if not decision.timed_out else f"no answer in {wait} s"})
                    return
                replacement = notice
                if decision.action == "edit":
                    replacement = handback_edited(source, decision.text)
                    if await self._edit_flagged(guard, decision.text):
                        reviews[-1] = "edited; the check flagged the edit, sent as you approved it"
                self.handbacks.add(text, replacement, "answer")
            self.stats.note_guard(route.entries, handed_back={"strategy": "answer", "review": "; ".join(reviews)})
        finally:
            for text in flagged:
                if (held := self._held.pop(handback_key(text), None)) is not None:
                    held.set()

    async def _wait_for_reviews(self, body, path: str) -> None:
        """A request carrying a flagged result whose hand-back is under review waits for the answer."""
        waiting: list[anyio.Event] = []

        def walk(value):
            if isinstance(value, str):
                if (held := self._held.get(handback_key(value))) is not None and held not in waiting:
                    waiting.append(held)
            elif isinstance(value, dict):
                for v in value.values():
                    walk(v)
            elif isinstance(value, list):
                for v in value:
                    walk(v)

        walk(body)
        if waiting:
            log.info("%s: waiting for the user's review of a hand-back", path)
        for held in waiting:
            with anyio.move_on_after(MAX_WAIT + 5):  # the review ends by its own deadline before this
                await held.wait()

    async def _edit_flagged(self, guard, text: str) -> bool:
        """The user's edit, checked: a flag is noted, the edit is theirs to send."""
        try:
            return bool(guard and await anyio.to_thread.run_sync(guard.check, [("your edit", text, True)], True))
        except Exception:
            return False

    async def _answer_locally(self, request: Request, t: Target, body: dict, route: LocalRoute, origin,
                              texts: dict | None = None) -> Response:
        """Local mode: the flagged request goes to a model on this Mac, as the agent wrote it (real
        values included: it stays on this Mac) but with that model's name, no tools and a note
        (local.py), and its answer goes back to the agent. Never the agent's API key. The servers
        for this check are tried in order; the first that answers with a 2xx gives the answer. A
        server that does not speak the agent's API is asked in Chat Completions instead."""
        path = request.url.path
        servers = self.local(route.check) if callable(self.local) else []
        refused = guard_summary(route.hits, "blocked")
        if not servers:
            self.stats.note_guard(route.entries, local_error="no local server is set for this check")
            self._record(path, "blocked", 403, origin=origin, guard=refused)
            return api_error(403, "permission_error", f"the {route.check} check flagged this request for a local model, "
                             "but none is set for it, so it was not sent. Add one with "
                             "`keepmum guard local add --model <name>`.")
        stream = wants_stream(t, body, request.url.query)
        local_origin = (origin[0] if origin else None, "local")
        after = None
        if route.check == "injection" and self._handback()[0] == "answer":
            flagged = {k: v for k, v in self._flagged_texts(route, texts).items()
                       if not self.handbacks.made(k) and not self.handbacks.kept(k)}  # kept: the user said local
            if flagged:
                async def after(answer: str, model: str) -> None:
                    await self._after_answer(route, flagged, answer, path, model)
        skills = self.local_skills(route.check) if callable(self.local_skills) else []
        skills = await anyio.to_thread.run_sync(skills_text, skills) if skills else ""
        failures: list[str] = []
        think = self._speed()[1]
        for server in servers:
            ways = (["chat"] if t.api not in LOCAL_PATHS or server.api == "chat"  # Gemini: always as Chat Completions
                    or (server.api == "auto" and (server.url, t.api) in self._chat_only)
                    else ["native", "chat"] if server.api == "auto" else ["native"])
            for way in ways:
                if way == "native":
                    data, target = prepare_local(body, t.api, server.model, route, skills), server.url + LOCAL_PATHS[t.api]
                else:
                    data, target = to_chat(body, t.api, server.model, route, skills, stream), server.url + LOCAL_CHAT_PATH
                headers = {"content-type": "application/json"}
                if t.api == "messages" and way == "native":
                    headers["anthropic-version"] = request.headers.get("anthropic-version", "2023-06-01")
                api = "chat" if way == "chat" else t.api
                if not think:
                    data = without_thinking(data, api)
                try:
                    resp = await self._send_local(target, headers, data)
                    if not think and resp.status_code in (400, 422):  # a server that refuses the fields that turn thinking off
                        await resp.aclose()
                        resp = await self._send_local(target, headers, {k: v for k, v in data.items()
                                                                        if k not in thinking_fields(api)})
                except httpx.HTTPError as e:
                    failures.append(f"{server.url} did not answer ({type(e).__name__})")
                    break  # the next server
                if not 200 <= resp.status_code < 300:
                    try:  # some servers (mlx_lm.server: HTTP/1.0) close the connection mid-body
                        detail = _error_text(await resp.aread())
                    except httpx.HTTPError:
                        detail = ""
                    await resp.aclose()
                    if way == "native" and len(ways) > 1 and resp.status_code in (400, 404, 405, 422, 501):
                        # It does not speak this API, or not this request's shape of it (Ollama's
                        # Responses API refuses item types it does not know): ask in Chat Completions.
                        log.info("%s: %s at %s refused the agent's API (HTTP %d%s); asking in Chat Completions",
                                 path, server.model, server.url, resp.status_code, f": {detail}" if detail else "")
                        continue
                    failures.append(f"{server.model} at {server.url}: HTTP {resp.status_code}" + (f" ({detail})" if detail else ""))
                    break
                content = None
                if not stream:
                    try:
                        content = await resp.aread()
                    except httpx.HTTPError as e:
                        failures.append(f"{server.model} at {server.url}: the answer broke off ({type(e).__name__})")
                        break
                    finally:
                        await resp.aclose()
                if way == "chat" and len(ways) > 1:
                    self._chat_only.add((server.url, t.api))
                if failures:
                    log.warning("%s: %s; answered by the next local server", path, "; ".join(failures))
                self.stats.note_guard(route.entries, answered_by={"model": server.model, "url": server.url,
                                                                  "api": way, "passed_over": failures})
                self._later(self._keep_loaded(server))
                # Gemini: as Code Assist wraps it when the request came wrapped, and as a JSON array for
                # a streamGenerateContent asked without alt=sse.
                shape = {"wrapped": isinstance(body.get("request"), dict),
                         "array": t.path.endswith(":streamGenerateContent") and not stream} if t.api == "gemini" else {}
                return await self._local_answer(resp, content, path, server, way, t.api, local_origin, route, after, shape)
        log.error("%s: no local server answered (%s); not sent anywhere else", path, "; ".join(failures))
        self.stats.note_guard(route.entries, local_error="no local server answered: " + "; ".join(failures))
        # 403, not 502: agents retry a 5xx (Claude Code and agy for minutes), and no retry helps until
        # the user starts the local model. It is a refusal: keepmum sends this request nowhere else.
        self._record(path, "blocked", 403, origin=origin, guard=refused)
        return api_error(403, "permission_error", f"the {route.check} check sent this request to a local model, and none "
                         f"answered: {'; '.join(failures)}. It was not sent anywhere else. Start the local model "
                         "(keepmum guard local), then try again.")

    async def _local_answer(self, resp: httpx.Response, content: bytes | None, path: str, server, way: str, api: str,
                            origin, route: LocalRoute, after=None, shape: dict | None = None) -> Response:
        """A local server's 2xx answer, passed back as it is or, from Chat Completions, in the
        agent's API: `content` when read whole, else streamed from `resp`. `after`, if given, gets
        the answer's text once the agent has it all (handback.py)."""
        hits = guard_summary(route.hits, "local")
        how = "" if way == "native" else ", as Chat Completions"
        headers = self._response_headers(resp)
        if way == "chat":
            headers = {k: v for k, v in headers.items() if k.lower() not in ("content-type", "content-length")}
        if content is None:
            translate = ChatStream(api, server.model, wrapped=bool((shape or {}).get("wrapped"))) if way == "chat" else None

            async def body():
                sent, size = [], 0
                try:
                    async for chunk in resp.aiter_raw():
                        out = translate.feed(chunk) if translate else chunk
                        if after and size < MAX_ANSWER_BYTES:
                            sent.append(out)
                            size += len(out)
                        yield out
                except httpx.HTTPError as e:  # too late for another server: end what was sent
                    log.warning("%s: the local model at %s broke off its answer (%s)", path, server.url, type(e).__name__)
                finally:
                    await resp.aclose()
                    self._record(path, "local", resp.status_code, origin=origin, guard=hits)
                    log.info("%s -> local model %s at %s%s: %d (streamed)", path, server.model, server.url, how, resp.status_code)
                if translate:
                    sent.append(end := translate.end())
                    yield end
                if after:
                    await after(answer_text(api, b"".join(sent), True), server.model)

            if translate:
                headers["content-type"] = "text/event-stream"
            return StreamingResponse(body(), status_code=resp.status_code, headers=headers)
        if way == "chat":
            try:
                content = json.dumps(from_chat(json.loads(content), api, server.model, **(shape or {})),
                                     ensure_ascii=False).encode()
            except (ValueError, AttributeError, TypeError, IndexError):
                pass  # not JSON: passed back as it is
            headers["content-type"] = "application/json"
        self._record(path, "local", resp.status_code, origin=origin, guard=hits)
        log.info("%s -> local model %s at %s%s: %d", path, server.model, server.url, how, resp.status_code)
        if after:
            await after(answer_text(api, content, False), server.model)
        return Response(content, status_code=resp.status_code, headers=headers)

    async def _check_later(self, guard, items, path: str, origin, kind: str = "clean", request: str | None = None,
                           texts: dict | None = None) -> None:
        """Warn mode, after the request is sent: record new hits; a failure is logged and nothing more."""
        try:
            hits = await anyio.to_thread.run_sync(guard.check, items, False)
        except Exception as e:
            log.warning("%s: %s check did not run (%s)", path, guard.name, type(e).__name__)
            return
        if hits:
            self._record_hits(guard, hits, "warned", path, origin, self._shown(hits, items, texts))
            self.stats.record_flagged(path, hits, origin, kind=kind, request=request)

    async def _send_local(self, target: str, headers: dict, data: dict) -> httpx.Response:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8", "replace")
        return await self.client.send(self.client.build_request("POST", target, headers=headers, content=payload), stream=True)

    async def _keep_loaded(self, server) -> None:
        """After a request to an Ollama server, how long it keeps the model loaded ([guard]
        local_keep_loaded): its Messages and Responses APIs take no keep_alive, its own API does, and
        a request without a prompt only sets it. Splash gets it when keepmum starts it (splashsvc)."""
        try:
            if server.url not in self._ollama:  # as local.kind tells it
                r = await self.client.get(server.url + "/api/version", timeout=5)
                self._ollama[server.url] = (r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json")
                                            and "version" in r.json()
                                            and (await self.client.get(server.url + "/api/tags", timeout=5)).status_code == 200)
            if not self._ollama[server.url]:
                return
            await self.client.post(server.url + "/api/generate",
                                   json={"model": server.model, "keep_alive": keep_alive(self._speed()[0])}, timeout=30)
        except (httpx.HTTPError, ValueError, AttributeError, TypeError) as e:
            log.debug("could not set how long %s keeps %s loaded (%s)", server.url, server.model, type(e).__name__)

    def _later(self, coro) -> None:
        import asyncio

        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def settle(self) -> None:
        """Wait for checks still running after their requests were sent (tests, shutdown)."""
        import asyncio

        # A finished task leaves the set only through its done callback, which the loop runs later;
        # gather over tasks that are all finished returns without yielding to the loop, so waiting
        # on the set alone spun forever (on Linux, where the checks often finish first).
        while self._background:
            self._background -= {t for t in self._background if t.done()}
            if self._background:
                await asyncio.gather(*list(self._background), return_exceptions=True)

    @staticmethod
    def _prompt(texts: dict | None) -> str | None:
        """What the user last typed in this request, as the Logs page may show it, or None."""
        if not texts:
            return None
        typed = texts.get("harm") or []
        return texts.get("shown", {}).get(typed[-1][1]) if typed else None

    def _shown(self, hits: list[dict], items, texts: dict | None) -> list[tuple[str | None, str | None]]:
        """For each hit, its text and the user's prompt, every masked value shown as its type
        ([log] context on; else Nones). In memory only: never in the audit log or the history."""
        shown = (texts or {}).get("shown") or {}
        prompt = self._prompt(texts)
        out = []
        for h in hits:
            at = h.get("at")
            text = shown.get(items[at][1]) if isinstance(at, int) and 0 <= at < len(items) else None
            out.append((text, None if h.get("check") == "harm" else prompt))  # harm's text is the prompt
        return out

    def _record_hits(self, guard, hits: list[dict], action: str, path: str, origin,
                     shown: list[tuple[str | None, str | None]] | None = None) -> list[dict]:
        """Log, count and audit hits: the check, source, score, length and categories, never the text.
        `shown`, per hit, is its text and the user's prompt for the Logs page alone. Returns the
        Logs page's entries for them."""
        for h in hits:
            cats = ", ".join(h.get("categories") or []) or "no category"
            what = (f"what the user typed ({cats})" if guard.name == "harm"
                    else f"a {h['source']} command in the reply ({cats})" if guard.name == "commands"
                    else f"a {h['source']} script in the reply ({cats})" if guard.name == "script"
                    else f"a {h['source'] or 'tool'} result")
            done = ("sent to the agent as a refusal" if guard.name == "commands" else "request not sent") \
                if action == "blocked" else "sent to the local model instead" if action == "local" else "sent (warn)"
            log.warning("%s: %s check flagged %s (score %.2f, %d chars%s): %s", path, guard.name, what, h["score"],
                        h["chars"], ", flagged before" if h["earlier"] else "", done)
        threshold = getattr(guard, "threshold", None)
        about = {"mode": guard.mode, "path": path,
                 "model": getattr(getattr(guard, "model", None), "label", None) or getattr(guard, "label", None),
                 "threshold": round(threshold, 3) if isinstance(threshold, (int, float)) else None}
        entries = self.stats.record_guard(hits, action, origin, about, shown)
        if self.audit is not None:
            client, provider = origin or (None, None)
            self.audit.append("guard", client=client, provider=provider, path=path, check=guard.name, action=action,
                              hits=[{k: (round(v, 3) if k == "score" else v) for k, v in h.items()
                                     if k in ("source", "score", "chars", "earlier", "categories")} for h in hits])
        return entries

    def _restore(self, data: dict, counts: Counter, api: str = "messages", policy: ToolPolicy | None = None) -> dict:
        policy = policy if policy is not None else self.policy
        with self._scan_lock:
            if api == "responses":
                return restore_responses(data, self.redactor.vault, policy, counts)
            if api in ("gemini", "gemini-plain"):
                return restore_gemini(data, self.redactor.vault, policy, counts)
            if not isinstance(data, dict):
                return data
            return restore_response(data, self.redactor.vault, policy, counts)

    def _reply_policy(self, path: str, origin, kind: str, texts: dict | None = None) -> ToolPolicy:
        """The tool policy for one reply: with the commands check on, each shell command in it is
        reviewed as it is restored; a hit is a note on the request (Activity, the flow's Flagged band).
        The script check always runs: a Codex script that sends computed values to a network tool
        is recorded, and the agent shows a note about it (the policy's `notes`), never refused."""
        from .codemode import ScriptCheck

        request = secrets.token_hex(6)
        notes: list[str] = []
        script = ScriptCheck()
        # A stream restores a call twice (its input's .done, then the whole item): each call is
        # reviewed, recorded and noted once, and the second time gets the same answer.
        reviewed: dict[str, object] = {}

        def review(name, value):
            key = name + "\0" + json.dumps(value, sort_keys=True, default=str)
            if key in reviewed:
                return reviewed[key]
            reviewed[key] = replaced = once(name, value)
            return replaced

        def once(name, value):
            replaced, hits = self.commands.review(name, value) if self.commands is not None else (None, [])
            if replaced is None:
                found, note = script.review(name, value, lambda n: rule_for(n, self.policy)[1])
                if found:
                    shown = [(None, self._prompt(texts) if self._context() else None)] * len(found)
                    self._record_hits(script, found, "warned", path, origin, shown)
                    self.stats.record_flagged(path, found, origin, kind=kind, request=request, action="warned")
                    notes.append(note)
            if hits:
                action = "blocked" if replaced is not None else "warned"
                shown = None
                if self._context():
                    from .commands import command_lines
                    from .redact import as_types

                    shown = [(as_types("\n".join(command_lines(name, value))), self._prompt(texts))] * len(hits)
                self._record_hits(self.commands, hits, action, path, origin, shown)
                self.stats.record_flagged(path, hits, origin, kind=kind, request=request, action=action)
            return replaced

        policy = self.policy.with_review(review)
        policy.notes = notes
        return policy

    async def _stream(self, url, headers, payload, path, t: Target, scan_ms, counts, new=0, outcome="forwarded",
                      origin=None, found=None, texts: dict | None = None, flight: int | None = None) -> Response:
        req = self.client.build_request("POST", url, headers=headers, content=payload)
        resp = await self.client.send(req, stream=True)
        restored = Counter()
        # A requested stream that succeeds is SSE; the ChatGPT backend sends it without a content type.
        is_sse = resp.status_code < 300
        kind = {"responses": ResponsesStreamRestorer, "gemini": GeminiStreamRestorer,
                "gemini-plain": GeminiStreamRestorer}.get(t.api, StreamRestorer)
        # Observed requests went out unmasked: there is nothing to restore, and no command is reviewed.
        policy = self._reply_policy(path, origin, "masked" if new else "clean", texts) if is_sse and outcome != "observed" else None
        restorer = kind(self.redactor.vault, policy, restored) if policy is not None else None

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
                self._record(path, outcome, resp.status_code, scan_ms, counts, restored, new=new, origin=origin, found=found,
                             flight=flight)
                log.info("%s -> %d (stream) redacted=%s restored=%s scan=%.0fms", path, resp.status_code,
                         dict(counts), dict(restored), scan_ms)

        return StreamingResponse(body(), status_code=resp.status_code, headers=self._response_headers(resp))

    # --- Plumbing -------------------------------------------------------------------

    @staticmethod
    def _url(request: Request, t: Target) -> str:
        return t.base + t.path + (f"?{request.url.query}" if request.url.query else "")

    async def _forward_raw(self, request: Request, t: Target, body: bytes = b"", outcome: str = "forwarded",
                           note: str = "no body") -> Response:
        path = request.url.path
        try:
            resp = await self.client.request(request.method, self._url(request, t),
                                             headers=self._headers(request, keep_encoding=True), content=body or None)
        except httpx.HTTPError as e:
            return api_error(502, "api_error", f"upstream request failed ({type(e).__name__})")
        self._record(path, outcome, resp.status_code, origin=self._origin(request, t))
        log.info("%s %s -> %d (not scanned: %s)", request.method, path, resp.status_code, note)
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
