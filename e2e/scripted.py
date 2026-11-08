"""A stand-in model that plays a case's script, in each agent's own wire format.

The real agent binary runs against it: Claude Code over the Messages API, Codex over the Responses
API. The model's tool calls are real, so the agent really runs them (writes files, runs commands)
after keepmum restored them. Only the model is scripted, which makes the run deterministic.

A script is a list of turns. Each turn is what the model says on one request:

  calls = [{shell = "mv a b"}, {write = "notes.md", content = "…"}, {read = "a.txt"},
           {edit = "a.yaml", old = "…", new = "…"}]
  say = "Done."

Which turn a request gets is decided by how many tool results it already carries, so requests the
agent makes on the side (titles, retries) never move the script on. Every placeholder in a reply is
split across two stream deltas, as a real stream may split it.
"""

from __future__ import annotations

import json
import re

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from keepmum.vault import PLACEHOLDER

from .servers import decode_body

# The workspace path as the agent tells the model (spaces and all): Codex's <cwd>, Claude Code's
# environment section. Searched in the request as JSON text, where a line ends in an escaped \n.
_CWD = (re.compile(r"<cwd>([^<]+)</cwd>"),
        re.compile(r"(?:Primary working directory|[Ww]orking directory): *(/.+?)(?:\\n|\\u|\"|$)"))


def _ev(data: dict) -> str:
    return f"event: {data['type']}\ndata: {json.dumps(data)}\n\n"


def _halves(text: str) -> list[str]:
    """text in two pieces, cut inside its first placeholder when it has one."""
    m = PLACEHOLDER.search(text) or re.search(r"(?i)keepmum[_-]", text)
    cut = (m.start() + 11) if m else len(text) // 2
    cut = min(max(cut, 0), len(text))
    return [p for p in (text[:cut], text[cut:]) if p]


class ScriptedModel:
    def __init__(self, turns: list[dict], render):
        """`render(template, cwd)`: a script string as the model writes it (placeholders, cwd)."""
        self.turns = turns
        self.render = render
        self.served: list[dict] = []  # per request: api, turn, outputs seen
        self.tools: set[str] = set()  # every tool the agent offered its model (tools.toml)
        self.requests: list[dict] = []  # the parsed bodies, as keepmum sent them
        self.app = Starlette(routes=[Route("/{path:path}", self.handle, methods=["GET", "POST"])])

    async def handle(self, request: Request) -> Response:
        path = "/" + request.path_params["path"]
        if path.endswith("/models"):
            return JSONResponse({"models": [], "data": []})
        if path == "/api/hello" or request.method in ("GET", "HEAD"):  # Claude Code's reachability check
            return JSONResponse({})
        raw = decode_body(await request.body(), request.headers.get("content-encoding"))
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            return JSONResponse({"error": "not json"}, status_code=400)
        if path.endswith("/count_tokens"):
            return JSONResponse({"input_tokens": 100})
        if path.endswith("/v1/messages"):
            self.requests.append(body)
            self.tools |= {t.get("name") for t in body.get("tools") or [] if isinstance(t, dict) and t.get("name")}
            return self._messages(body)
        if path.endswith("/responses"):
            self.requests.append(body)
            self.tools |= {n for n in self._tool_names(body) if n}
            return self._responses(body)
        return JSONResponse({"error": f"scripted model: no {path}"}, status_code=404)

    # --- which turn ---------------------------------------------------------------

    def _turn(self, outputs: int) -> tuple[int, dict]:
        done = 0
        for i, turn in enumerate(self.turns):
            if done >= outputs:
                return i, turn
            done += len(turn.get("calls", []))
        return len(self.turns), {"say": "Done."}

    @staticmethod
    def _cwd(body: dict) -> str:
        text = json.dumps(body, ensure_ascii=False)
        for pattern in _CWD:
            if m := pattern.search(text):
                return m.group(1).rstrip()
        return "."

    # --- Messages API (Claude Code) ------------------------------------------------------

    def _messages(self, body: dict) -> Response:
        if not body.get("tools"):  # a side request: a title, a quota check
            return self._anthropic([{"type": "text", "text": "ok"}], body)
        outputs = sum(1 for m in body.get("messages") or [] if isinstance(m.get("content"), list)
                      for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result")
        index, turn = self._turn(outputs)
        cwd = self._cwd(body)
        self.served.append({"api": "messages", "turn": index, "outputs": outputs, "cwd": cwd})
        blocks = []
        if turn.get("say"):
            blocks.append({"type": "text", "text": self.render(turn["say"], cwd)})
        for i, call in enumerate(turn.get("calls", [])):
            name, args = self._claude_call(call, cwd)
            blocks.append({"type": "tool_use", "id": f"toolu_e2e_{index}_{i}", "name": name, "input": args})
        return self._anthropic(blocks, body)

    def _claude_call(self, call: dict, cwd: str) -> tuple[str, dict]:
        def absolute(p):
            p = self.render(p, cwd)
            return p if p.startswith("/") else f"{cwd.rstrip('/')}/{p}"

        if "shell" in call:
            return "Bash", {"command": self.render(call["shell"], cwd), "description": "Run a command"}
        if "write" in call:
            return "Write", {"file_path": absolute(call["write"]), "content": self.render(call.get("content", ""), cwd)}
        if "read" in call:
            return "Read", {"file_path": absolute(call["read"])}
        if "edit" in call:
            return "Edit", {"file_path": absolute(call["edit"]), "old_string": self.render(call["old"], cwd),
                            "new_string": self.render(call["new"], cwd)}
        if "claude" in call:  # a raw call: {claude = {name = "Edit", input = {...}}}
            raw = call["claude"]
            return raw["name"], json.loads(self.render(json.dumps(raw["input"]), cwd))
        raise ValueError(f"unknown call {call}")

    def _anthropic(self, blocks: list[dict], body: dict) -> Response:
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        usage = {"input_tokens": 100, "output_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        if not body.get("stream"):
            return JSONResponse({"id": "msg_e2e", "type": "message", "role": "assistant", "model": body.get("model"),
                                 "content": blocks, "stop_reason": stop, "stop_sequence": None, "usage": usage})
        out = [_ev({"type": "message_start", "message": {"id": "msg_e2e", "type": "message", "role": "assistant",
                                                         "model": body.get("model"), "content": [], "stop_reason": None,
                                                         "stop_sequence": None, "usage": usage}})]
        for i, b in enumerate(blocks):
            if b["type"] == "text":
                out.append(_ev({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}}))
                for piece in _halves(b["text"]):
                    out.append(_ev({"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": piece}}))
            else:
                out.append(_ev({"type": "content_block_start", "index": i,
                                "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}}))
                for piece in _halves(json.dumps(b["input"])):
                    out.append(_ev({"type": "content_block_delta", "index": i,
                                    "delta": {"type": "input_json_delta", "partial_json": piece}}))
            out.append(_ev({"type": "content_block_stop", "index": i}))
        out.append(_ev({"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                        "usage": {"output_tokens": 10}}))
        out.append(_ev({"type": "message_stop"}))
        return Response("".join(out), media_type="text/event-stream")

    # --- Responses API (Codex) -------------------------------------------------------------

    @staticmethod
    def _tool_names(body: dict) -> set[str]:
        """The tools a Responses request offers, and those a code-mode `exec` script can call, as
        exec:<name> (declared in its description: `declare const tools: { apply_patch(…`)."""
        tools = [t for t in body.get("tools") or [] if isinstance(t, dict)]
        for item in body.get("input") or []:
            if isinstance(item, dict) and item.get("type") == "additional_tools":
                for ns in item.get("tools") or []:
                    tools += [t for t in ns.get("tools") or [] if isinstance(t, dict)]
        names = {t.get("name") for t in tools}
        for t in tools:
            if t.get("name") == "exec" and isinstance(t.get("description"), str):
                names |= {f"exec:{n}" for n in re.findall(r"declare const tools: \{ (\w+)\(", t["description"])}
        return names

    def _responses(self, body: dict) -> Response:
        items = body.get("input") if isinstance(body.get("input"), list) else []
        outputs = sum(1 for i in items if isinstance(i, dict)
                      and i.get("type") in ("function_call_output", "custom_tool_call_output"))
        index, turn = self._turn(outputs)
        cwd = self._cwd(body)
        code_mode = "exec" in self._tool_names(body) and "apply_patch" not in {t.get("name") for t in body.get("tools") or []}
        self.served.append({"api": "responses", "turn": index, "outputs": outputs, "cwd": cwd, "code_mode": code_mode})
        out_items = []
        if turn.get("say"):
            out_items.append({"type": "message", "id": f"msg_e2e_{index}", "role": "assistant", "status": "completed",
                              "content": [{"type": "output_text", "text": self.render(turn["say"], cwd), "annotations": []}]})
        for i, call in enumerate(turn.get("calls", [])):
            out_items.append(self._codex_call(call, cwd, code_mode, f"{index}_{i}"))
        return self._openai(out_items)

    def _codex_call(self, call: dict, cwd: str, code_mode: bool, tag: str) -> dict:
        def custom(name, text):
            return {"type": "custom_tool_call", "id": f"ctc_e2e_{tag}", "call_id": f"call_e2e_{tag}", "name": name,
                    "input": text, "status": "completed"}

        def function(name, args):
            return {"type": "function_call", "id": f"fc_e2e_{tag}", "call_id": f"call_e2e_{tag}", "name": name,
                    "arguments": json.dumps(args), "status": "completed"}

        if "codex" in call:  # a raw call: {codex = "<JavaScript for exec>"}
            return custom("exec", self.render(call["codex"], cwd))
        if "read" in call:
            call = {"shell": f"cat {call['read']}"}
        if "shell" in call:
            cmd = self.render(call["shell"], cwd)
            if code_mode:
                return custom("exec", f"const r = await tools.exec_command({{cmd: {json.dumps(cmd)}}});\ntext(r);")
            return function("exec_command", {"cmd": cmd})
        if "edit" in call:
            old, new = (self.render(call[k], cwd).rstrip("\n").split("\n") for k in ("old", "new"))
            patch = "*** Begin Patch\n*** Update File: " + self.render(call["edit"], cwd) + "\n@@\n" \
                    + "".join(f"-{line}\n" for line in old) + "".join(f"+{line}\n" for line in new) + "*** End Patch\n"
            if code_mode:
                return custom("exec", f"text(await tools.apply_patch({json.dumps(patch)}));")
            return custom("apply_patch", patch)
        if "write" in call:
            body = self.render(call.get("content", ""), cwd)
            lines = body.split("\n")
            if lines and lines[-1] == "":
                lines.pop()
            patch = "*** Begin Patch\n*** Add File: " + self.render(call["write"], cwd) + "\n" \
                    + "".join(f"+{line}\n" for line in lines) + "*** End Patch\n"
            if code_mode:
                return custom("exec", f"text(await tools.apply_patch({json.dumps(patch)}));")
            return custom("apply_patch", patch)
        raise ValueError(f"unknown call {call}")

    @staticmethod
    def _openai(items: list[dict]) -> Response:
        usage = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110, "input_tokens_details": {"cached_tokens": 0},
                 "output_tokens_details": {"reasoning_tokens": 0}}
        out = [_ev({"type": "response.created", "response": {"id": "resp_e2e", "status": "in_progress", "output": []}})]
        for n, item in enumerate(items):
            if item["type"] == "message":
                text = item["content"][0]["text"]
                out.append(_ev({"type": "response.output_item.added", "output_index": n, "item": {**item, "content": []}}))
                for piece in _halves(text):
                    out.append(_ev({"type": "response.output_text.delta", "item_id": item["id"], "output_index": n,
                                    "content_index": 0, "delta": piece}))
                out.append(_ev({"type": "response.output_text.done", "item_id": item["id"], "output_index": n,
                                "content_index": 0, "text": text}))
            else:
                field, kind = ("input", "custom_tool_call_input") if item["type"] == "custom_tool_call" \
                    else ("arguments", "function_call_arguments")
                out.append(_ev({"type": "response.output_item.added", "output_index": n, "item": {**item, field: ""}}))
                for piece in _halves(item[field]):
                    out.append(_ev({"type": f"response.{kind}.delta", "item_id": item["id"], "output_index": n, "delta": piece}))
                out.append(_ev({"type": f"response.{kind}.done", "item_id": item["id"], "output_index": n, field: item[field]}))
            out.append(_ev({"type": "response.output_item.done", "output_index": n, "item": item}))
        out.append(_ev({"type": "response.completed", "response": {"id": "resp_e2e", "status": "completed",
                                                                   "output": items, "usage": usage}}))
        return Response("".join(out), media_type="text/event-stream")
