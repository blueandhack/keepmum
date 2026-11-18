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

import contextlib
import gzip
import hmac
import json
import logging
import re
import secrets
import sys
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
from .handback import REMOVED, harm_messages, harm_pieces, harm_wrap, harm_written, nothing_left, only_removed, sentences, without
from .handback import _key as handback_key
from .handback import newest as newest_input
from .handback import edited as handback_edited
from .handback import messages as handback_messages
from .handback import notice as handback_notice
from .handback import wrap as handback_wrap
from .handback import written as handback_written
from .review import HELD as REVIEW_HELD
from .review import MAX_WAIT, WRITTEN_ACTIONS, Piece, Review, Reviews, TurnReview
from .review import PIECE_ACTIONS
from .review import applies as review_applies
from .review import assist_messages as review_assist_messages
from .review import assisted as review_assisted
from .local import DEFAULT_MAX_TOKENS as LOCAL_MAX_TOKENS
from .local import NO_THINKING
from .local import keep_alive, thinking_fields, without_thinking
from .local import ChatStream, NativeFilter, answer_text, from_chat, skills_text, to_chat, without_tool_calls
from .local import LocalRoute
from .local import prepare as prepare_local
from .redact import Redactor
from .responses import ResponsesStreamRestorer, restore_responses
from .restore import DEFAULT_POLICY, StreamRestorer, ToolPolicy, restore_response, rule_for, take_tools, take_urls
from .stats import Stats, client_of, flows, guard_summary, provider_of
from .tools import BULK, CONTACTS, definitions

# httpcore asks for sniffio on every call that takes a lock, to tell asyncio from trio, and goes on
# with asyncio when it is not there. Python does not remember a module it could not find, so each
# ask searched every folder on the path: about 3 a request, 5% of keepmum's CPU under load. Marked
# missing once, the import fails at once, as it did anyway.
try:
    import sniffio  # noqa: F401
except ImportError:
    sys.modules.setdefault("sniffio", None)  # type: ignore[assignment]

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
    return {"send": "approved", "edit": "edited", "withhold": "withheld", "local": "kept local",
            "original": "sent the original, as you chose"}[decision.action]


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


def failure(e: Exception) -> str:
    """An exception for the log: its type, and for a module that would not load, which one. Other
    messages are left out: they can quote the request."""
    name = getattr(e, "name", None) if isinstance(e, ImportError) else None
    return f"{type(e).__name__}: {name}" if name else type(e).__name__


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


def too_long(raw: bytes) -> bool:
    """A local server's error saying the request is longer than its model's context."""
    text = raw[:2000].decode("utf-8", "replace").lower()
    return any(k in text for k in ("context_length", "context window", "context length", "exceeds the context",
                                   "maximum context", "too many tokens", "n_ctx"))


def _without_tool_calls(content: bytes, api: str) -> bytes:
    """A local model's whole answer in the agent's API, with any tool call in it made a note."""
    try:
        reply = json.loads(content)
    except ValueError:
        return content
    fixed = without_tool_calls(reply, api)
    return content if fixed is reply else json.dumps(fixed, ensure_ascii=False).encode()


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
                 local_skills=None, handback=None, review=None, speed=None, handback_harm=None,
                 harm_instructions=None):
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
        # The user's own instruction for that rewrite ("": keepmum's), a callable.
        self._harm_instructions = harm_instructions if callable(harm_instructions) else (lambda: "")
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
        # A harm rewrite under way, by what was typed: another request carrying the same text (Codex's
        # task title comes with the turn) waits for it and takes its outcome, not a review of its own.
        self._rewriting: dict[bytes, anyio.Lock] = {}
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
            return JSONResponse({"reviews": self.reviews.pending(), "turns": self.reviews.turns(), "mode": mode, "wait": wait,
                                 "timeout": timeout})
        if request.method == "POST" and path.count("/") == 4 and path.endswith("/assist"):
            try:
                ask = json.loads(await request.body() or b"{}")
            except ValueError:
                return api_error(400, "invalid_request_error", "the body must be JSON: {\"text\": …, \"request\": …}")
            return await self._assist_review(path.split("/")[3], str(ask.get("text") or ""), str(ask.get("request") or ""))
        if request.method == "POST" and path.count("/") == 4 and path.endswith("/draft"):
            try:
                ask = json.loads(await request.body() or b"{}")
            except ValueError:
                return api_error(400, "invalid_request_error", "the body must be JSON: {\"piece\": …, \"kind\": …}")
            return await self._draft_route(path.split("/")[3], ask if isinstance(ask, dict) else {})
        if request.method == "POST" and path.count("/") == 3:
            try:
                answer = json.loads(await request.body() or b"{}")
            except ValueError:
                return api_error(400, "invalid_request_error", "the body must be JSON: {\"action\": …}")
            why = self.reviews.decide(path.rsplit("/", 1)[-1], str(answer.get("action")), answer.get("text"),
                                      answer.get("pieces"))
            if why:
                return api_error(404 if why.startswith("no such") else 400, "invalid_request_error", why)
            return JSONResponse({"ok": True})
        return api_error(405, "invalid_request_error", "GET /_keepmum/reviews, or POST /_keepmum/reviews/<id>")

    async def _assist_review(self, rid: str, draft: str, asked: str) -> Response:
        """POST /_keepmum/reviews/<id>/assist {"text": the box, "request": what to change}: the text as
        the check's local model changed it, {"text", "model"}. Nothing is decided: the user still
        sends it, as an edit (checked and noted like any edit). The review waits while it writes."""
        review = self.reviews.get(rid)
        if review is None:
            return api_error(404, "invalid_request_error", "no such review: it was answered, or ran out of time")
        if not asked.strip():
            return api_error(400, "invalid_request_error", "say what to change")
        with self.reviews.assisting(review):
            got = await self._write_locally("edit", draft, review.source, None, "", "", review.path, check=review.check,
                                            messages=review_assist_messages(review, draft, asked))
        if got is None:
            return api_error(502, "api_error", "no local model answered: see Settings › Local models")
        out, server = got
        log.info("%s: the local model %s edited a review's text as the user asked", review.path, server.model)
        return JSONResponse({"text": review_assisted(out), "model": server.model})

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
        REVIEW_HELD.set((origin[0], time.time()))  # a review of this request ends before its agent gives up
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
        # What is new in this request (what was just typed, the latest tool results) is asked about
        # again, whatever was answered for the same text before; the history keeps its answers.
        fresh = newest_input(body)
        original, (body, _) = body, self._handed_back(body, seen, skip=fresh)
        handled: list[tuple[LocalRoute, dict]] = []  # each check's route and what its hand-back did
        while True:
            try:
                masked, counts, new, found, texts = await anyio.to_thread.run_sync(self._redact, body, t.api, origin)
            except Exception as e:  # any detector or redaction failure: block, never send raw
                log.error("blocked %s: scan failed (%s)", path, failure(e))
                self._record(path, "blocked", 500, origin=origin)
                return api_error(500, "api_error", "could not scan this request, so it was not sent")
            # keepmum's own replacements are not tool output: what the local model wrote in them was
            # checked on its own (`_hand_back`), and their wording reads like an injection to the model.
            # New text is checked on the first scan even when it is one (an original the user sent).
            texts["fresh"] = fresh
            first = not handled
            typed_in = texts.get("typed_in") or {}
            texts["injection"] = [i for i in texts.get("injection") or []
                                  if not self.handbacks.made(i[1]) or first and i[1] in fresh]
            texts["harm"] = [i for i in texts.get("harm") or []
                             if not self.harm_handbacks.made(i[1]) or first and typed_in.get(i[1]) in fresh]
            refused = await self._guarded(texts, path, origin, kind="masked" if new else "clean", api=t.api)
            if isinstance(refused, list):
                if handled:  # scanned again, and what the local model wrote (or the user) was flagged
                    log.info("%s: the %s check flagged what went in a flagged part's place too; answered locally",
                             path, refused[0].check)
                    for route, handed in handled:
                        self.stats.note_guard(route.entries, handed_back={**handed, "kept": "what the local model wrote was flagged too"})
                    return await self._answer_locally(request, t, original, refused[0], origin, texts)
                # Reviewed whole, in one window: every flagged piece, each answered by the user.
                if self._review()[0] == "turn" and self.reviews.present():
                    outcome = await self._review_turn(refused, texts, path)
                    if any("kept" in o for o in outcome.values()):
                        for route in refused:
                            self.stats.note_guard(route.entries, handed_back=outcome.get(route.check) or {"kept": "kept local"})
                        return await self._answer_locally(request, t, original, refused[0], origin, texts)
                    handled = [(route, outcome.get(route.check) or {}) for route in refused]
                    seen = []
                    body, _ = self._handed_back(original, seen)
                    continue
                # Each check hands back its part, what was typed first; the turn goes to the cloud only
                # if every part can: one kept local keeps the turn local, and nothing more is asked.
                for route in refused:
                    handed = await self._hand_back(route, texts, path)
                    if not handed or handed.get("kept"):
                        if handed:
                            self.stats.note_guard(route.entries, handed_back=handed)
                        for done, was in handled:
                            self.stats.note_guard(done.entries, handed_back={
                                **was, "kept": f"the {route.check} check kept the turn on this Mac"})
                        return await self._answer_locally(request, t, original, route, origin, texts)
                    handled.append((route, handed))
                seen = []
                body, _ = self._handed_back(original, seen)
                continue  # scanned again: what the local model wrote is checked like any result
            if refused is not None:
                return refused
            break
        for route, handed in handled:
            self.stats.note_guard(route.entries, handed_back=handed)
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
            self.redactor.aside = False
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
                       "shown": self.redactor.shown, "typed_in": self.redactor.typed_in,
                       "aside": self.redactor.aside})
            self.redactor.tool_results, self.redactor.user_texts, self.redactor.shown = [], [], {}
            self.redactor.typed_in, self.redactor.aside = {}, False
        if self.tools is not None:
            try:
                self.tools.observe(definitions(body, api), origin[0] if origin else None,
                                   lambda name: rule_for(name, self.policy)[1] != "restore")
            except Exception as e:  # the registry never stops a request
                log.warning("tool list not recorded (%s)", type(e).__name__)
        return result

    async def _guarded(self, texts: dict, path: str, origin, observing: bool = False, kind: str = "clean",
                       api: str | None = None) -> Response | list[LocalRoute] | None:
        """Run each guard on its texts: None to send the request, the refusal, or where it goes instead.
        Block and local mode check before the request goes, and every such check runs, whatever another
        found: text is scored once (a text seen before is not scored again), so a check skipped now would
        let what it would have flagged through on the next try. A check in block mode that flags it, or
        one that fails, refuses the request; else each check in local mode that flags it gives its route
        to the local model (local.py), the harm check's first: what was typed kept local, no result needs
        handing back. Warn mode, and every mode while observing, checks after it is sent, so a warning
        adds no delay. `kind` is how the request goes out (masked, clean, observed): a warning carries it,
        with an id of the request, so the flow diagram moves the request to Flagged."""
        request = secrets.token_hex(6)
        found, failed = [], None
        for guard in self.guards:
            items = texts.get(guard.name) or []
            mode = guard.mode
            if mode == "off" or not items:
                continue
            if mode in ("block", "local") and not observing:
                try:
                    hits = await anyio.to_thread.run_sync(guard.check, items, True)
                except Exception as e:  # it cannot vouch for what it did not check
                    log.error("blocked %s: %s check failed (%s)", path, guard.name, type(e).__name__)
                    failed = failed or api_error(500, "api_error", f"the {guard.name} check failed, so this request was not "
                                                 f"sent (`keepmum guard {guard.name} warn` sends such requests with a warning)")
                    continue
                if hits:
                    found.append((guard, items, hits))
            else:
                self._later(self._check_later(guard, items, path, origin, kind, request, texts))
        if not found and failed is None:
            return None
        aside = bool((texts or {}).get("aside"))  # Codex's task title: its turn has the Logs row
        if failed is None and api in APIS_LOCAL and all(g.mode == "local" for g, _, _ in found):
            routes = [LocalRoute(g.name, hits, self._record_hits(g, hits, "local", path, origin, self._shown(hits, items, texts), aside))
                      for g, items, hits in found]
            return sorted(routes, key=lambda r: r.check != "harm")
        for g, items, hits in found:
            self._record_hits(g, hits, "blocked", path, origin, self._shown(hits, items, texts), aside)
        if failed is not None:
            self._record(path, "blocked", 500, origin=origin)
            return failed
        self._record(path, "blocked", 403, origin=origin,
                     guard=[s for _, _, hits in found for s in guard_summary(hits, "blocked")])
        return api_error(403, "permission_error", " Also, ".join(self._refusal(g.name, hits) for g, _, hits in found))

    @staticmethod
    def _refusal(check: str, hits: list[dict]) -> str:
        """What the agent is told when a check refuses its request."""
        if check == "harm":
            cats = sorted({c for h in hits for c in h.get("categories") or []})
            return ("the harm check flagged what you typed" + (f" ({', '.join(cats)})" if cats else "")
                    + ", so this request was not sent. Rephrase it (rewind, or /clear), or run "
                    "`keepmum guard harm warn` to send such requests with a warning.")
        sources = ", ".join(sorted({h["source"] or "a tool" for h in hits}))
        return (f"the injection check found a likely prompt injection in what {sources} returned, so this "
                "request was not sent. Remove that result from the conversation (rewind, or /clear), or run "
                "`keepmum guard injection warn` to send such requests with a warning.")

    def _handed_back(self, body: dict, seen: list | None = None, skip: frozenset[str] = frozenset()) -> tuple[dict, int]:
        """The body with the results handed back so far replaced: while the injection check is in
        local mode and hands back. Turned off, or set to stay, the request goes as the agent wrote it.
        `seen` gets the replaced results' keys (Handbacks.first_sent); `skip`, what is left as it is."""
        guard = next((g for g in self.guards if g.name == "injection"), None)
        n = 0
        turn = self._review()[0] == "turn"  # sent on from a turn's review, whatever the defaults say
        if guard is not None and guard.mode == "local" and (self._handback()[0] != "stay" or turn):
            body, n = self.handbacks.apply(body, seen, skip)
        harm = next((g for g in self.guards if g.name == "harm"), None)
        if harm is not None and harm.mode == "local" and (self._handback_harm() == "rewrite" or turn):
            body, m = self.harm_handbacks.apply(body, seen, skip)
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
        fresh = (texts or {}).get("fresh") or frozenset()
        if not flagged or any(self.handbacks.kept(text, fresh=text in fresh) for text in flagged):
            return None  # what the local model wrote for it was flagged before: it stays local
        if all(self.handbacks.went(text, fresh=text in fresh) is not None for text in flagged):
            for text in flagged:  # answered moments ago, for a request carrying it too (a retry)
                self._note_reuse(path, text, self.handbacks, text in fresh)
                self.handbacks.add(text, self.handbacks.went(text, fresh=text in fresh), strategy)
            return {"strategy": strategy, "review": "as before, for the same result"}
        if strategy == "withhold":
            for text, source in flagged.items():
                self.handbacks.add(text, handback_notice(strategy, source), strategy)
            log.info("%s: the flagged result withheld; the request goes on to the cloud", path)
            return {"strategy": strategy}
        skills = self.local_skills("handback") if callable(self.local_skills) else []
        skills = await anyio.to_thread.run_sync(skills_text, skills) if skills else ""
        prompt = (texts.get("harm") or [(None, None, None)])[-1][1]
        done, server, reviews, withheld = [], None, [], False
        guard = next((g for g in self.guards if g.name == "injection"), None)
        for text, source in flagged.items():
            why: dict = {}
            got = await self._write_locally(strategy, text, source, prompt, instructions, skills, path, why=why)
            if got is None and why.get("too_long"):
                # Too long for the local model: no summary or rewrite. Under review, the user decides on
                # keepmum's notice instead; without, the turn stays local, as when the model fails.
                mode, wait, timeout = self._review()
                if not (review_applies(mode, strategy) and self.reviews.present()):
                    return None
                note = handback_notice("withhold", source)
                reason = (f"It is {len(text):,} characters, more than the local model ({why['too_long']}) can read "
                          f"at once, so it wrote no {strategy}.")
                log.info("%s: the flagged result is too long to %s; holding keepmum's notice for review (up to %d s)",
                         path, "summarise" if strategy == "summary" else strategy, wait)
                decision = await self.reviews.ask(Review("withhold", source, path, text, note, why["too_long"], wait,
                                                         reason=reason), timeout)
                reviews.append(_reviewed(decision, wait))
                if decision.action == "local":
                    for t in flagged:
                        self.handbacks.keep_local(t)
                    return {"strategy": strategy, "model": why["too_long"], "review": reviews[-1], "too_long": True,
                            "kept": "you kept it on this Mac" if not decision.timed_out else f"no answer in {wait} s"}
                if decision.action == "original":  # the user's call, after a warning: the result as it was
                    done.append((text, text))
                elif decision.action == "edit":
                    if await self._edit_flagged(guard, decision.text):
                        reviews[-1] = "edited; the check flagged the edit, sent as you approved it"
                    done.append((text, handback_edited(source, decision.text, read=False)))
                else:  # send, withhold: the notice
                    done.append((text, note))
                withheld = True
                continue
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
                if decision.action == "original":  # the user's call, after a warning: the result as it was
                    done.append((text, text))
                    continue
                if decision.action == "edit":
                    out = decision.text
                    if await self._edit_flagged(guard, out):  # the user's own text: noted if flagged, theirs to send
                        reviews[-1] = "edited; the check flagged the edit, sent as you approved it"
            done.append((text, handback_wrap(strategy, source, out, custom=bool(instructions))))
        for text, replacement in done:
            self.handbacks.add(text, replacement, strategy)
            self.handbacks.went(text, replacement)
        if server is None:  # every result was too long: what went is the notice, or the user's text
            log.info("%s: the flagged result was too long for the local model; it went on as reviewed", path)
            return {"strategy": "withhold", "too_long": True, **({"review": "; ".join(reviews)} if reviews else {})}
        log.info("%s: the local model %s at %s wrote a %s of the flagged result; checking it", path, server.model,
                 server.url, strategy)
        return {"strategy": strategy, "model": server.model, "url": server.url,
                **({"too_long": True} if withheld else {}), **({"review": "; ".join(reviews)} if reviews else {})}

    async def _hand_back_harm(self, route: LocalRoute, texts: dict, path: str) -> dict | None:
        """[guard] local_handback_harm "rewrite" (handback.py): what the user typed, with what the harm
        check flagged taken out by the local model, in place of what they typed, so the turn can go to
        the cloud. Removal only: what the local model wrote is checked by the harm check. Under review
        ([guard] local_handback_review) the user decides on every rewrite, one that failed a check too,
        told why, and on a message the local model wrote no rewrite of (review.py); without, a rewrite
        still flagged, one that changed words, or nothing left keeps the turn local. No skills go with it
        (see below). A text is rewritten and reviewed once at a time: another request carrying it (Codex's task
        title comes with the turn) waits for that and, within REUSE_S, goes the same way; later, new in a
        request, it is asked about again (handback.newest). None leaves it to the local model."""
        if self._handback_harm() != "rewrite":
            return None
        flagged = self._flagged_texts(route, texts)
        whole = (texts or {}).get("typed_in") or {}
        fresh = (texts or {}).get("fresh") or frozenset()
        if not flagged or any(self.harm_handbacks.kept(text, fresh=whole.get(text) in fresh) for text in flagged):
            return None
        guard = next((g for g in self.guards if g.name == "harm"), None)
        done, server, reviews, again = [], None, [], 0
        for text in flagged:
            full = whole.get(text)
            if not full or text not in full:  # what was typed is not one piece of its message: kept local
                return None
            new = full in fresh
            async with self._one_rewrite(text):
                if self.harm_handbacks.kept(text, fresh=new):
                    return None  # kept local while this request waited on another carrying it
                if (mine := self.harm_handbacks.went(text, fresh=new)) is not None:
                    self._note_reuse(path, text, self.harm_handbacks, new)
                    done.append((full, full.replace(text, mine, 1), mine))  # what went for it before
                    again += 1
                    continue
                got = await self._rewrite_typed(text, full, flagged, guard, path, reviews)
                if not isinstance(got, tuple):
                    return got
                server = got[1] or server
                done.append(got[0])
        for full, replacement, mine in done:
            self.harm_handbacks.add(full, replacement, "rewrite")
            self.harm_handbacks.mark_made(mine)
        if again == len(done):
            log.info("%s: what was typed went on as its rewrite did before", path)
            return {"strategy": "rewrite", "review": "as before, for the same text"}
        if server is None:
            log.info("%s: what was typed went on as the user wrote it in review", path)
            return {"strategy": "rewrite", **({"review": "; ".join(reviews)} if reviews else {})}
        log.info("%s: the local model %s at %s took the flagged part out of what was typed, and it went on as checked or reviewed",
                 path, server.model, server.url)
        return {"strategy": "rewrite", "model": server.model, "url": server.url,
                **({"review": "; ".join(reviews)} if reviews else {})}

    async def _review_turn(self, routes: list, texts: dict, path: str) -> dict[str, dict]:
        """[guard] local_handback_review "turn" (review.py): the turn's flagged pieces, what was typed and
        each tool result, in one review. The local model drafts each piece's default meanwhile (checked
        as a hand-back is); the user answers each (rewrite or summary, the original, or removed) and
        sends the turn on, or keeps it here. What each check's part came to, for the Logs page, by
        check; one with "kept" keeps the turn on this Mac."""
        whole = (texts or {}).get("typed_in") or {}
        strategy, instructions = self._handback()
        tool_default = {"summary": "summary", "withhold": "remove", "answer": "remove"}.get(strategy, "rewrite")
        pieces: list[Piece] = []
        for route in routes:
            for text, source in self._flagged_texts(route, texts).items():
                if route.check == "harm":
                    full = whole.get(text)
                    if not full or text not in full:  # what was typed is not one piece of its message
                        return {"harm": {"strategy": "turn", "kept": "what was typed is not one piece of its message"}}
                    pieces.append(Piece("", "harm", text, whole=full))
                else:
                    pieces.append(Piece("", "injection", text, source=source, default=tool_default))
        stores = {"harm": self.harm_handbacks, "injection": self.handbacks}
        async with contextlib.AsyncExitStack() as stack:
            # One review of a typed text at a time: Codex's task-title request carries it too, and waits.
            for text in sorted({p.text for p in pieces if p.check == "harm"}, key=handback_key):
                await stack.enter_async_context(self._one_rewrite(text))
            fresh = (texts or {}).get("fresh") or frozenset()

            def new(p: Piece) -> bool:
                return (p.whole if p.check == "harm" else p.text) in fresh

            if any(stores[p.check].kept(p.text, fresh=new(p)) for p in pieces):
                for p in pieces:
                    self._note_reuse(path, p.text, stores[p.check], new(p))
                return {r.check: {"strategy": "turn", "kept": "you kept it on this Mac"} for r in routes}
            again = [p for p in pieces if stores[p.check].went(p.text, fresh=new(p)) is not None]
            for p in again:  # answered with the request that first carried it
                self._note_reuse(path, p.text, stores[p.check], new(p))
                mine = stores[p.check].went(p.text, fresh=new(p))
                if p.check == "harm":
                    self.harm_handbacks.add(p.whole, p.whole.replace(p.text, mine, 1), "rewrite")
                    self.harm_handbacks.mark_made(mine)
                else:
                    self.handbacks.add(p.text, mine, "rewrite")
            pieces = [p for p in pieces if p not in again]
            if not pieces:
                log.info("%s: what was typed went on as it did before", path)
                return {r.check: {"strategy": "turn", "review": "as before, for the same text"} for r in routes}
            for i, p in enumerate(pieces, 1):
                p.id = str(i)
            _, wait, timeout = self._review()
            checks = {p.check for p in pieces}
            send = ("harm" not in checks or self._handback_harm() == "rewrite") and ("injection" not in checks or strategy != "stay")
            turn = TurnReview(path, pieces, wait, send=send, context=(texts.get("harm") or [(None, None, None)])[-1][1])
            log.info("%s: holding the turn for the user's review in one window, %d flagged piece%s (up to %d s)", path,
                     len(pieces), "" if len(pieces) == 1 else "s", wait)
            async with anyio.create_task_group() as tg:
                for p in pieces:
                    if p.default in WRITTEN_ACTIONS:
                        tg.start_soon(self._draft, turn, p, p.default)
                decision = await self.reviews.ask_turn(turn, timeout)
                tg.cancel_scope.cancel()
            said = f"no answer in {wait} s: " if decision.timed_out else ""
            if decision.action == "local":
                for p in pieces:
                    stores[p.check].keep_local(p.text)
                # What each piece's draft had come to, for the Logs page: the user may ask why it stayed.
                drafts = "; ".join(f"{p.source or 'what you typed'}: " + (
                    f"{p.kind} {'ready' if p.failed is None else 'did not pass'}" if p.draft is not None
                    else p.failed or "no draft") for p in pieces)
                log.info("%s: the turn was kept on this Mac (%s; %s)", path, said.rstrip(": ") or "you chose it", drafts)
                kept = "you kept it on this Mac" if not decision.timed_out else f"no answer in {wait} s"
                return {c: {"strategy": "turn", "review": f"{said}kept local; {drafts}", "kept": kept}
                        for c in checks | {r.check for r in routes}}
            guards = {g.name: g for g in self.guards}
            out: dict[str, dict] = {}
            for p in pieces:
                a = decision.pieces[p.id]
                action, text = a["action"], a["text"]
                note = {"original": "the original, as you chose", "remove": "removed"}.get(action, action + (" (yours)" if a["edited"] else ""))
                if a["edited"] and await self._edit_flagged(guards.get(p.check), text):
                    note += "; the check flagged your text, sent as you approved it"
                if p.check == "harm":
                    mine = p.text if action == "original" else harm_wrap(REMOVED if action == "remove" else text)
                    self.harm_handbacks.went(p.text, mine)
                    self.harm_handbacks.add(p.whole, p.whole.replace(p.text, mine, 1), "original" if action == "original" else "rewrite")
                    self.harm_handbacks.mark_made(mine)
                else:
                    replacement = (p.text if action == "original" else handback_notice("withhold", p.source) if action == "remove"
                                   else handback_wrap(action, p.source, text, custom=a["edited"] or bool(instructions)))
                    self.handbacks.add(p.text, replacement, {"remove": "withhold"}.get(action, action))
                    self.handbacks.went(p.text, replacement)
                was = out.setdefault(p.check, {"strategy": {"remove": "withhold"}.get(action, action), "review": said + "reviewed with the turn"})
                was["review"] += f"; {p.source or 'what you typed'}: {note}"
                if p.model:
                    was.setdefault("model", p.model)
            log.info("%s: the turn goes on as reviewed: %s", path, "; ".join(o["review"] for o in out.values()))
            return out

    async def _draft(self, turn: TurnReview, piece: Piece, kind: str, request: str | None = None,
                     base: str | None = None) -> None:
        """A `kind` (rewrite or summary) of one piece of a turn under review, from its check's local
        model, checked as a hand-back is; with `request`, `base` (the user's box) changed as they ask,
        and checked the same. Only the latest asked for lands. The turn waits while it is written."""
        piece.asked += 1
        mine = piece.asked
        piece.drafting = True
        try:
            with self.reviews.assisting(turn):
                got = await self._write_piece(turn, piece, kind, request, base)
        finally:
            if piece.asked == mine:
                piece.drafting = False
        if piece.asked == mine:
            piece.draft, piece.kind, piece.failed, piece.model = got

    async def _write_piece(self, turn: TurnReview, piece: Piece, kind: str, request: str | None, base: str | None):
        """(text or None, kind, why it did not pass or None, model) for `_draft`. A second draft of the
        same kind (Write Again) is sampled: at temperature 0 the harm rewrite would be the same again."""
        harm = piece.check == "harm"
        path = turn.path
        skills = self.local_skills("harm_handback" if harm else "handback") if callable(self.local_skills) else []
        skills = await anyio.to_thread.run_sync(skills_text, skills) if skills else ""
        msgs = review_assist_messages(piece, base or "", request) if request else None
        why: dict = {}
        again = piece.kind == kind and piece.draft is not None and not request
        if harm:
            got = await self._write_locally("rewrite", piece.text, None, None, self._harm_instructions(), skills, path,
                                            check="harm", why=why, messages=msgs, temperature=0.7 if again else None)
        else:
            got = await self._write_locally(kind, piece.text, piece.source, turn.context, self._handback()[1], skills, path,
                                            why=why, messages=msgs)
        if got is None:
            if harm and not request:  # no local model: keepmum's own removal, if it passes
                guard = next((g for g in self.guards if g.name == "harm"), None)
                if (mine := await self._by_sentence(piece.text, guard)) is not None:
                    return mine, kind, None, "keepmum, sentence by sentence"
            failed = (f"It is {len(piece.text):,} characters, more than the local model ({why['too_long']}) can read at once."
                      if why.get("too_long") else f"No local model wrote a {kind}.")
            log.info("%s: no %s for the turn's review: %s", path, kind, failed)
            return None, kind, failed, why.get("too_long") or ""
        out, server = got
        out = review_assisted(out) if request else out
        guard = next((g for g in self.guards if g.name == piece.check), None)
        try:
            if harm:
                out = harm_written(out)
                failed = ("The local model changed words in it, not only took the flagged part out." if not only_removed(piece.text, out)
                          else "Nothing was left once the flagged part was out." if nothing_left(out) else None)
                if failed is None and guard:
                    hits = await anyio.to_thread.run_sync(guard.check, [(None, x, True) for x in harm_pieces(out)], True)
                    failed = "The harm check flagged the rewrite too." if hits else None
                # The user asked for this change: theirs to judge. Otherwise, keepmum's own removal.
                if failed and not request and (mine := await self._by_sentence(piece.text, guard)) is not None:
                    log.info("%s: the local model's rewrite did not pass (%s); keepmum took out the sentences the harm check "
                             "flags", path, failed)
                    return mine, kind, None, f"{server.model}, then keepmum sentence by sentence"
                log.info("%s: a rewrite of what was typed for the turn's review: %s", path, failed or "passed")
                return out, kind, failed, server.model
            hits = await anyio.to_thread.run_sync(guard.check, [("the local model", out, True)], True) if guard else []
            log.info("%s: a %s of a tool result for the turn's review: %s", path, kind, "flagged" if hits else "passed")
            return out, kind, "The injection check flagged what the local model wrote too." if hits else None, server.model
        except Exception as e:
            log.warning("%s: a draft for the turn's review was not checked (%s)", path, type(e).__name__)
            return out, kind, "keepmum could not check it.", server.model

    async def _by_sentence(self, text: str, guard) -> str | None:
        """keepmum's own removal, for when the local model's rewrite of what was typed fails a check:
        each sentence of it scored by the harm check on its own, the flagged ones taken out whole, the
        rest as typed. Then checked as any rewrite is: as a whole and sentence by sentence. None when
        that does not pass either: one sentence, none flagged alone, all of them, or the rest flagged."""
        parts = sentences(text)
        if guard is None or len(parts) < 2:
            return None
        try:
            hits = await anyio.to_thread.run_sync(guard.check, [(None, p.strip(), True) for p in parts], True)
            flagged = {h["at"] for h in hits if isinstance(h.get("at"), int)}
            if not flagged or len(flagged) == len(parts):
                return None
            out = without(parts, flagged)
            if nothing_left(out) or not only_removed(text, out):
                return None
            still = await anyio.to_thread.run_sync(guard.check, [(None, x, True) for x in harm_pieces(out)], True)
        except Exception as e:
            log.warning("keepmum's own rewrite of what was typed was not checked (%s)", type(e).__name__)
            return None
        return None if still else out

    async def _draft_route(self, rid: str, ask: dict) -> Response:
        """POST /_keepmum/reviews/<id>/draft {"piece", "kind": rewrite | summary, "request"?, "text"?}: a
        new draft of one piece of a turn under review, or, with "request", the user's "text" changed as
        they ask. The piece as it is then (review.Piece.public)."""
        turn = self.reviews.get_turn(rid)
        piece = turn.piece(str(ask.get("piece"))) if turn else None
        if piece is None:
            return api_error(404, "invalid_request_error", "no such review or piece: it was answered, or ran out of time")
        kind = ask.get("kind") or ("rewrite" if piece.check == "harm" else piece.default)
        if kind not in WRITTEN_ACTIONS or kind not in PIECE_ACTIONS[piece.check]:
            return api_error(400, "invalid_request_error", f"kind must be {' or '.join(a for a in WRITTEN_ACTIONS if a in PIECE_ACTIONS[piece.check])}")
        request = str(ask.get("request") or "").strip() or None
        await self._draft(turn, piece, kind, request, str(ask.get("text") or "") if request else None)
        return JSONResponse(piece.public())

    @staticmethod
    def _note_reuse(path: str, text: str, store, new: bool) -> None:
        """Log a past answer used for new text: only ever within REUSE_S of it (Codex's task title)."""
        if new and (when := store.answered(text)) is not None:
            log.info("%s: used the answer you gave %.0f s ago for the same text, new in this request (the agent's "
                     "own request with the turn, or a retry)", path, time.time() - when)

    @contextlib.asynccontextmanager
    async def _one_rewrite(self, text: str):
        """One harm rewrite of a text at a time: a request that brings it while another's rewrite or
        review is under way waits for that (`_rewriting`)."""
        key = handback_key(text)
        lock = self._rewriting.setdefault(key, anyio.Lock())
        try:
            async with lock:
                yield
        finally:
            if not lock.locked() and not lock.statistics().tasks_waiting:
                self._rewriting.pop(key, None)

    async def _rewrite_typed(self, text: str, full: str, flagged, guard, path: str, reviews: list):
        """The rewrite of one flagged text, reviewed if review is on: ((full, replacement, mine),
        server or None) to send, or what `_hand_back_harm` returns to keep the turn local."""
        server = None
        # The user's instruction, or keepmum's, and only the skills the user picked for this place
        # ("harm_handback"): a skill for answering or for tool results can turn the model against the
        # user's own task. Qwen3.6 35B, given the jailbreak-then-task message of Nov 15, 2026 with
        # keepmum-local-turns after the instruction, kept the jailbreak and took the task out in 2 of 4
        # runs; with the instruction alone, 1 miss in 30 over five such messages. Whatever either says,
        # the rewrite is checked below.
        skills = self.local_skills("harm_handback") if callable(self.local_skills) else []
        skills = await anyio.to_thread.run_sync(skills_text, skills) if skills else ""
        why: dict = {}
        got = await self._write_locally("rewrite", text, None, None, self._harm_instructions(), skills, path,
                                        check="harm", why=why)
        mode, wait, timeout = self._review()
        asking = review_applies(mode, "rewrite") and self.reviews.present()
        if got is None:
            if not asking:
                return None
            model, out, sendable = why.get("too_long") or "none answered", "", False
            failed = (f"It is {len(text):,} characters, more than the local model ({why['too_long']}) can read at once, "
                      "so it wrote no rewrite." if why.get("too_long") else "No local model wrote a rewrite.")
            kept = "no rewrite"
        else:
            out, server = got
            model = server.model
            out = harm_written(out)
            changed = not only_removed(text, out)
            try:
                hits = [] if changed or not guard else await anyio.to_thread.run_sync(
                    guard.check, [(None, piece, True) for piece in harm_pieces(out)], True)
            except Exception as e:
                log.warning("%s: the local rewrite was not checked (%s); answered locally", path, type(e).__name__)
                return None
            empty = nothing_left(out)
            if (changed or hits or empty) and (mine := await self._by_sentence(text, guard)) is not None:
                log.info("%s: the local model's rewrite did not pass; keepmum took out the sentences the harm check flags", path)
                out, changed, hits, empty = mine, False, [], False
            kept = ("the rewrite changed what was typed, not only took a part out" if changed
                    else "the harm check flagged the rewrite too" if hits
                    else "nothing was left once the flagged part was out" if empty else None)
            failed = {"the rewrite changed what was typed, not only took a part out":
                      "The local model changed words in it, not only took the flagged part out.",
                      "the harm check flagged the rewrite too": "The harm check flagged the rewrite too.",
                      "nothing was left once the flagged part was out": "Nothing was left once the flagged part was out."}.get(kept)
            sendable = not empty
            if kept and not asking:
                for t in flagged:
                    self.harm_handbacks.keep_local(t)
                log.info("%s: %s; answered locally", path, kept)
                return {"strategy": "rewrite", "model": model, "url": server.url, "kept": kept}
        if asking:
            log.info("%s: holding the rewrite of what was typed for the user's review (up to %d s)%s", path, wait,
                     f"; {kept}" if kept else "")
            decision = await self.reviews.ask(Review("rewrite", None, path, text, out, model, wait, reason=failed,
                                                     check="harm", sendable=sendable), timeout)
            reviews.append(_reviewed(decision, wait))
            # No answer under "send" sends it as written, but only a rewrite that passed: never one
            # that failed a check, or nothing at all, without the user's say.
            if decision.timed_out and decision.action == "send" and kept:
                reviews[-1] = f"no answer in {wait} s: kept local, since {kept}"
            if decision.action == "original":  # the user's call, after a warning: what they typed, as typed
                log.info("%s: what was typed went on as typed, as the user chose in review", path)
                mine = self.harm_handbacks.went(text, text)
                return (full, full, mine), server
            if decision.action not in ("send", "edit") or (decision.timed_out and kept) or not (sendable or decision.action == "edit"):
                for t in flagged:
                    self.harm_handbacks.keep_local(t)
                log.info("%s: the rewrite of what was typed was kept on this Mac (%s)", path, reviews[-1])
                return {"strategy": "rewrite", "model": model, **({"url": server.url} if server else {}),
                        "review": reviews[-1],
                        "kept": "you kept it on this Mac" if not decision.timed_out else f"no answer in {wait} s"}
            if decision.action == "edit":
                out = decision.text
                if await self._edit_flagged(guard, out):  # the user's own words: noted if flagged, theirs to send
                    reviews[-1] = "edited; the check flagged the edit, sent as you approved it"
            elif kept:
                reviews[-1] = f"{reviews[-1]}, though {kept}"
        mine = self.harm_handbacks.went(text, harm_wrap(out))
        return (full, full.replace(text, mine, 1), mine), server

    @staticmethod
    def _flagged_texts(route: LocalRoute, texts: dict | None) -> dict[str, str | None]:
        """The texts of a route's hits, each with its source (the tool)."""
        items = (texts or {}).get(route.check) or []
        return {items[h["at"]][1]: items[h["at"]][0] for h in route.hits
                if isinstance(h.get("at"), int) and 0 <= h["at"] < len(items)}

    async def _write_locally(self, strategy: str, text: str, source: str | None, prompt: str | None,
                             instructions: str, skills: str, path: str, check: str = "injection",
                             why: dict | None = None, messages: list | None = None,
                             temperature: float | None = None) -> tuple[str, object] | None:
        """A summary or rewrite of a flagged result (or, for the harm check, of what was typed) from the
        check's local servers, the first that writes one, in Chat Completions (every local server speaks
        it), with no tools."""
        servers = self.local(check) if callable(self.local) else []
        failures = []
        for server in servers:
            data = {"model": server.model, "stream": False, "max_tokens": LOCAL_MAX_TOKENS,
                    "messages": messages or (harm_messages(text, skills, instructions) if check == "harm"
                                             else handback_messages(strategy, text, source, prompt, instructions, skills)),
                    **NO_THINKING,
                    # Taking words out of what was typed has one right answer: the model's most likely one. Sampled,
                    # Qwen3.6 35B kept the jailbreak about 1 run in 10, and the turn stayed local; at 0, 24 of 24
                    # right over eight messages (Nov 15, 2026).
                    **({"temperature": temperature} if temperature is not None
                       else {"temperature": 0} if check == "harm" and messages is None else {})}
            try:
                resp = await self.client.post(server.url + LOCAL_CHAT_PATH, json=data)
                # A server that refuses the fields that turn thinking off: ask again without them. Not
                # when the text is too long for it: that answer would be the same.
                if resp.status_code in (400, 422) and not too_long(resp.content):
                    resp = await self.client.post(server.url + LOCAL_CHAT_PATH,
                                                  json={k: v for k, v in data.items() if k not in NO_THINKING})
            except httpx.HTTPError as e:
                failures.append(f"{server.url} did not answer ({type(e).__name__})")
                continue
            if not 200 <= resp.status_code < 300:
                detail = _error_text(resp.content)
                long = too_long(resp.content)
                failures.append(f"{server.model} at {server.url}: HTTP {resp.status_code}" + (f" ({detail})" if detail else "")
                                + (f"; the flagged result is {len(text):,} characters, more than its context holds" if long else ""))
                if long and why is not None:
                    why.setdefault("too_long", server.model)
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
                if decision.action == "original":  # the user's call, after a warning: the result as it was
                    replacement = text
                elif decision.action == "edit":
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
                headers = {"content-type": "application/json", "accept-encoding": "identity"}  # NativeFilter reads it
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
            if way == "chat":
                translate = ChatStream(api, server.model, wrapped=bool((shape or {}).get("wrapped")))
            else:  # the agent's own API: passed on as it comes, but never a tool call (local.NativeFilter)
                translate = NativeFilter(api) if api in ("messages", "responses") else None

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
        elif api in ("messages", "responses"):
            content = _without_tool_calls(content, api)
            headers.pop("content-length", None)
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
                     shown: list[tuple[str | None, str | None]] | None = None, aside: bool = False) -> list[dict]:
        """Log, count and audit hits: the check, source, score, length and categories, never the text.
        `shown`, per hit, is its text and the user's prompt for the Logs page alone. Returns the
        Logs page's entries for them. A request an agent makes aside the turn (harm.aside: Codex's task
        title, with the prompt inside) is audited, but its turn has the warning and the Logs page's row."""
        if aside:
            log.info("%s: the agent's own request for a task title carries what the %s check flagged in its turn: %s",
                     path, guard.name, "sent to the local model instead" if action == "local" else "not sent")
        for h in hits if not aside else ():
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
        entries = self.stats.record_guard(hits, action, origin, about, shown) if not aside else []
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
