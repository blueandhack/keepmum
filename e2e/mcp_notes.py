"""A stub MCP server for e2e cases: `notes`, over stdio, standard library only.

It stands in for a network tool (a mail or chat connector): what it receives is what would leave
the Mac. Each call's arguments are appended, as one JSON line, to the file $KEEPMUM_E2E_MCP_LOG,
which the oracle reads (expect.mcp). `lookup` returns a fixed text from $KEEPMUM_E2E_MCP_REPLY, so a
case can check that a server gets back a value it returned itself ("own").
"""

from __future__ import annotations

import json
import os
import sys

TOOLS = [
    {"name": "send_note", "description": "Send a note to the team.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "lookup", "description": "Look up a record by name.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
]


def _log(entry: dict) -> None:
    path = os.environ.get("KEEPMUM_E2E_MCP_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # a notification
    if method == "initialize":
        result = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "notes", "version": "1.0.0"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = msg.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        _log({"tool": name, "arguments": args})
        text = os.environ.get("KEEPMUM_E2E_MCP_REPLY", "no record") if name == "lookup" else "sent"
        result = {"content": [{"type": "text", "text": text}], "isError": False}
    elif method == "ping":
        result = {}
    elif method in ("resources/list", "resources/templates/list", "prompts/list"):
        result = {method.split("/")[0] if "templates" not in method else "resourceTemplates": []}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reply = handle(msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
