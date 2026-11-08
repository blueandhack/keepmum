"""An ASGI app on a real port in a thread, and the tap that records what leaves keepmum."""

from __future__ import annotations

import gzip
import io
import socket
import threading
import time
from dataclasses import dataclass, field

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route


class ThreadServer:
    """uvicorn in a thread on 127.0.0.1, on a port picked when it is bound."""

    def __init__(self, app):
        import uvicorn

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)

    def start(self) -> ThreadServer:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)
        try:
            self.sock.close()
        except OSError:
            pass


def decode_body(raw: bytes, encoding: str | None) -> bytes:
    """A request body as sent, decompressed: keepmum sends zstd to chatgpt.com."""
    encoding = (encoding or "").lower()
    try:
        if encoding == "zstd":
            import zstandard

            return zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw), read_across_frames=True).read()
        if encoding == "gzip":
            return gzip.decompress(raw)
    except Exception:
        return raw
    return raw


# Never written to a result: they carry the agent's login.
SECRET_HEADERS = frozenset({"authorization", "x-api-key", "x-goog-api-key", "cookie", "chatgpt-account-id"})
HOP = frozenset({"host", "content-length", "connection", "transfer-encoding", "accept-encoding"})


@dataclass
class Exchange:
    target: str
    method: str
    path: str
    query: str
    headers: dict[str, str]
    body: bytes
    status: int = 0
    reply: bytes = b""
    meta: dict = field(default_factory=dict)

    def sent(self) -> bytes:
        """Everything that left this Mac in the request: path, query, headers, body."""
        head = "\n".join(f"{k}: {v}" for k, v in self.headers.items())
        return f"{self.method} {self.path}?{self.query}\n{head}\n\n".encode() + self.body


class Tap:
    """Sits between keepmum and the provider. keepmum's upstreams point at /<target>/...; each
    request is recorded (decompressed) and sent on to that target: a real provider's URL, or an
    ASGI app (the scripted model). Replies are recorded too, decoded."""

    def __init__(self, targets: dict[str, object]):
        self.targets = targets
        self.exchanges: list[Exchange] = []
        self._lock = threading.Lock()
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(900, connect=15))
        self.app = Starlette(routes=[Route("/{target}/{path:path}", self.handle,
                                           methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])])

    async def handle(self, request: Request) -> Response:
        name = request.path_params["target"]
        path = "/" + request.path_params["path"]
        raw = await request.body()
        ex = Exchange(name, request.method, path, request.url.query,
                      {k: ("…" if k in SECRET_HEADERS else v) for k, v in request.headers.items()},
                      decode_body(raw, request.headers.get("content-encoding")))
        with self._lock:
            self.exchanges.append(ex)
        target = self.targets.get(name)
        if target is None:
            ex.status = 502
            return Response(f"e2e tap: no target {name}", status_code=502)
        headers = {k: v for k, v in request.headers.items() if k not in HOP}
        if isinstance(target, str):
            client, base = self.client, target.rstrip("/")
        else:  # an ASGI app: the scripted model
            client, base = httpx.AsyncClient(transport=httpx.ASGITransport(app=target), base_url="http://scripted.test",
                                             timeout=60), "http://scripted.test"
        url = base + path + (f"?{request.url.query}" if request.url.query else "")
        upstream = await client.send(client.build_request(request.method, url, headers=headers, content=raw), stream=True)
        ex.status = upstream.status_code
        out_headers = {k: v for k, v in upstream.headers.items()
                       if k.lower() not in HOP | {"content-encoding"}}
        chunks: list[bytes] = []

        async def relay():
            try:
                async for chunk in upstream.aiter_bytes():
                    chunks.append(chunk)
                    yield chunk
            finally:
                ex.reply = b"".join(chunks)
                await upstream.aclose()
                if client is not self.client:
                    await client.aclose()

        return StreamingResponse(relay(), status_code=upstream.status_code, headers=out_headers)
