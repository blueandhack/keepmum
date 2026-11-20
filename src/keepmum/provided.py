"""Results of tools the provider runs itself, for the injection check.

Anthropic's server tools (web search, web fetch, code execution) and its MCP connector run at
Anthropic: their results come back inside the reply, never as a tool result the agent sends, so the
injection check did not read them. What keepmum can read of them: a search result's title and
address (the page's text is encrypted, for the model alone), the passages the reply quotes from those
pages, a fetched page's text, a code run's output, an MCP server's reply. OpenAI's web search (Codex)
and Gemini's grounding return no page text: only the titles of the pages they used.

They are checked twice: when the reply comes in, as a warning, since the reply has already reached
the agent and the model has read them (ReplyCollector); and, with the Messages API, when the agent
sends them back with the conversation, where block and local mode apply as to any tool's result
(Redactor._block)."""

from __future__ import annotations

import json

# Fields that are no text a page or a tool wrote: ids, the encrypted page, the address it was fetched from.
_SKIP = frozenset({"type", "id", "tool_use_id", "encrypted_content", "encrypted_index", "page_age", "file_id",
                   "signature", "retrieved_at", "media_type", "return_code", "is_error", "url"})


def source(name: str) -> str:
    """How a result shows on the Logs page and in the audit log: `web_fetch (provider)`."""
    return f"{name} (provider)"


def is_result(kind: object) -> bool:
    """A server tool's or the MCP connector's result block: `web_search_tool_result`, `mcp_tool_result`…
    (`tool_result` is the agent's own tool's, checked as it always was)."""
    return isinstance(kind, str) and kind.endswith("_tool_result") and kind != "tool_result"


def call_name(block: dict) -> str:
    """The tool a `server_tool_use` or `mcp_tool_use` block calls: `web_search`, `linear list_issues`."""
    name = str(block.get("name") or "tool")
    server = block.get("server_name")
    return f"{server} {name}" if isinstance(server, str) and server else name


def _strings(value, out: list[str]) -> None:
    if isinstance(value, str):
        if value.strip():
            out.append(value)
    elif isinstance(value, dict):
        if value.get("type") == "base64":
            return  # a PDF or a picture a page was: no text to read here
        for k, v in value.items():
            if k not in _SKIP:
                _strings(v, out)
    elif isinstance(value, list):
        for v in value:
            _strings(v, out)


def result_text(block: dict) -> str | None:
    """What of one result block can be read: for a search, each result's title and address; else
    every text in it (a fetched page, a run's output, a server's reply). None for an error."""
    content = block.get("content")
    if isinstance(content, dict) and str(content.get("type", "")).endswith("_error"):
        return None
    if block.get("type") == "web_search_tool_result":
        if not isinstance(content, list):
            return None
        out = [f"{r.get('title') or ''}\n{r.get('url') or ''}".strip() for r in content
               if isinstance(r, dict) and r.get("type") == "web_search_result"]
    else:
        out = []
        _strings(content, out)
    text = "\n\n".join(s for s in out if s)
    return text or None


def cited_text(block: dict) -> str | None:
    """The passages a text block quotes from the pages a web search found (the readable part of
    those pages), each after its page's title."""
    out = [f"{c.get('title') or ''}: {c['cited_text']}".strip(": ") for c in block.get("citations") or []
           if isinstance(c, dict) and c.get("type") == "web_search_result_location" and isinstance(c.get("cited_text"), str)]
    return "\n".join(out) or None


def messages_items(content) -> list[tuple[str, str]]:
    """(source, text) for each provider-run result in a Messages reply's or assistant turn's content."""
    if not isinstance(content, list):
        return []
    names: dict[str, str] = {}
    out: list[tuple[str, str]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in ("server_tool_use", "mcp_tool_use") and isinstance(block.get("id"), str):
            names[block["id"]] = call_name(block)
        elif is_result(kind):
            if text := result_text(block):
                out.append((source(names.get(block.get("tool_use_id")) or kind.removesuffix("_tool_result")), text))
        elif kind == "text" and (text := cited_text(block)):
            out.append((source("web_search"), text))
    return _unique(out)


def responses_items(output) -> list[tuple[str, str]]:
    """OpenAI's web search: the titles and addresses of the pages a reply cites (no page text comes back)."""
    lines = []
    for item in output if isinstance(output, list) else []:
        for part in (item.get("content") or []) if isinstance(item, dict) else []:
            for a in (part.get("annotations") or []) if isinstance(part, dict) else []:
                if isinstance(a, dict) and a.get("type") == "url_citation":
                    lines.append(f"{a.get('title') or ''}\n{a.get('url') or ''}".strip())
    text = "\n\n".join(dict.fromkeys(x for x in lines if x))
    return [(source("web_search"), text)] if text else []


def gemini_items(data) -> list[tuple[str, str]]:
    """Gemini's grounding: the titles and addresses of the pages it used (no page text comes back),
    from a reply or a stream's chunks (each repeats the sources so far). The Antigravity CLI's replies
    wrap Gemini's in `response`."""
    chunks = data if isinstance(data, list) else [data]
    candidates = [c for d in chunks if isinstance(d, dict) for c in (d.get("response", d).get("candidates") or [])]
    lines = []
    for cand in candidates:
        meta = (cand.get("groundingMetadata") or {}) if isinstance(cand, dict) else {}
        for chunk in meta.get("groundingChunks") or []:
            web = chunk.get("web") if isinstance(chunk, dict) else None
            if isinstance(web, dict):
                lines.append(f"{web.get('title') or ''}\n{web.get('uri') or ''}".strip())
    text = "\n\n".join(dict.fromkeys(x for x in lines if x))
    return [(source("google_search"), text)] if text else []


def items_of(api: str, data) -> list[tuple[str, str]]:
    """The provider-run results in a whole (not streamed) reply."""
    if not isinstance(data, dict):
        return []
    if api == "responses":
        return responses_items(data.get("output"))
    if api in ("gemini", "gemini-plain"):
        return gemini_items(data)
    return messages_items(data.get("content"))


def _unique(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out = []
    for s, t in items:
        if t not in seen:
            seen.add(t)
            out.append((s, t))
    return out


class ReplyCollector:
    """Picks the provider-run results out of a streamed reply (server-sent events) as it passes:
    each block as it starts (a server tool's result comes whole in its start event) and the
    citations added to text blocks. The text itself is not kept."""

    def __init__(self, api: str):
        self.api = api
        self._buf = b""
        self._blocks: dict[object, dict] = {}
        self._output: list = []
        self._gemini: list[dict] = []

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            try:
                event = json.loads(line[5:])
            except ValueError:
                continue
            if isinstance(event, dict):
                self._event(event)

    def _event(self, ev: dict) -> None:
        if self.api == "responses":
            if ev.get("type") in ("response.completed", "response.done"):
                self._output = (ev.get("response") or {}).get("output") or []
            elif ev.get("type") == "response.output_item.done" and isinstance(ev.get("item"), dict):
                self._output.append(ev["item"])
        elif self.api in ("gemini", "gemini-plain"):
            self._gemini.append(ev)
        else:
            kind = ev.get("type")
            if kind == "content_block_start":
                block = ev.get("content_block")
                # Only the blocks that can hold a provider-run result, or quote one.
                if isinstance(block, dict) and (block.get("type") in ("server_tool_use", "mcp_tool_use", "text")
                                                or is_result(block.get("type"))):
                    self._blocks[ev.get("index")] = dict(block, citations=list(block.get("citations") or []))
            elif kind == "content_block_delta":
                delta = ev.get("delta") or {}
                block = self._blocks.get(ev.get("index"))
                if delta.get("type") == "citations_delta" and block is not None:
                    block["citations"].append(delta.get("citation"))

    def items(self) -> list[tuple[str, str]]:
        if self.api == "responses":
            return responses_items(self._output)
        if self.api in ("gemini", "gemini-plain"):
            return gemini_items(self._gemini)
        return messages_items([self._blocks[k] for k in self._blocks])
