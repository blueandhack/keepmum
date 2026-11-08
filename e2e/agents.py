"""The coding agents, run for real: how to point each at keepmum, and how to read what it did.

Each run keeps the user's login but not their setup: no user config, MCP servers, hooks or plugins,
so a case runs the same on any Mac. The agent's HOME is the real one (its login lives there);
keepmum's HOME is the run's own (salt, config, audit log).
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Transcript:
    final: str = ""  # the agent's last answer
    shown: list[str] = field(default_factory=list)  # everything else the user sees: messages, reasoning summaries
    commands: list[str] = field(default_factory=list)  # commands the agent ran, as it ran them
    tool_inputs: list[str] = field(default_factory=list)  # file edits and other tool inputs, as run
    mcp_inputs: list[str] = field(default_factory=list)  # MCP tool calls: placeholders are right there (expect.mcp_*)
    # Tools that send what they get off this Mac without keepmum scanning it again: web search and
    # fetch, a browser. A placeholder is right there.
    network_inputs: list[str] = field(default_factory=list)
    web_calls: int = 0  # every web tool call, those that go back through keepmum too (expect.web_calls_min)
    errors: list[str] = field(default_factory=list)


def _clean_env(real_home: str) -> dict[str, str]:
    """The environment of the user's terminal: none of the variables a host agent (this one) set."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE_CODE_", "CLAUDECODE", "CLAUDE_AGENT_SDK", "CLAUDE_PID", "CLAUDE_EFFORT"))
           and k not in ("ANTHROPIC_BASE_URL", "KEEPMUM_PF_SERVE", "HF_HOME")}
    env["HOME"] = real_home
    return env


class Agent:
    name = ""
    binary = ""
    api = ""  # the provider route keepmum serves it on: anthropic, chatgpt, openai, antigravity
    modes = ("scripted", "live")  # scripted needs the scripted model to speak its provider's API
    mcp = True  # it can be given the stub MCP server for one run

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def version(self) -> str:
        import subprocess

        try:
            return subprocess.run([self.binary, "--version"], capture_output=True, text=True, timeout=20,
                                  stdin=subprocess.DEVNULL).stdout.strip()
        except Exception:
            return "?"

    def command(self, ws: Path, port: int, prompt: str, real_home: str, out: Path, model: str | None,
                extra: list[str], mcp: dict | None = None) -> tuple[list[str], dict[str, str]]:
        """`mcp`: one stdio MCP server to give the agent, {name, command, args, env}."""
        raise NotImplementedError

    def transcript(self, stdout: str, out: Path) -> Transcript:
        raise NotImplementedError


class Codex(Agent):
    name, binary = "codex", "codex"

    @property
    def api(self) -> str:
        from keepmum.clients import codex_auth_mode

        return "openai" if codex_auth_mode() == "apikey" else "chatgpt"

    def command(self, ws, port, prompt, real_home, out, model, extra, mcp=None):
        from keepmum.clients import codex_args

        servers = []
        if mcp:
            key = f"mcp_servers.{mcp['name']}"
            env = ",".join(f"{k}={json.dumps(v)}" for k, v in mcp["env"].items())
            servers = ["-c", f"{key}.command={json.dumps(mcp['command'])}", "-c", f"{key}.args={json.dumps(mcp['args'])}",
                       "-c", f"{key}.env={{{env}}}", "-c", f'{key}.default_tools_approval_mode="approve"']
        argv = [shutil.which(self.binary), "exec", "--json", "--skip-git-repo-check", "--ephemeral",
                "--ignore-user-config", "-s", "workspace-write", "-C", str(ws), "-o", str(out / "final.txt"),
                *codex_args(port), *servers, *(["-m", model] if model else []), *extra, prompt]
        return argv, _clean_env(real_home)

    def transcript(self, stdout, out):
        t = Transcript()
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            item = ev.get("item") or {}
            kind = item.get("type")
            if ev.get("type") == "item.completed":
                if kind == "agent_message":
                    t.shown.append(item.get("text", ""))
                elif kind == "reasoning":
                    t.shown.append(item.get("text", ""))
                elif kind == "command_execution":
                    t.commands.append(item.get("command", ""))
                    t.shown.append(item.get("aggregated_output", ""))
                elif kind == "file_change":
                    t.tool_inputs.append(json.dumps(item.get("changes", []), ensure_ascii=False))
                elif kind == "mcp_tool_call":
                    t.mcp_inputs.append(json.dumps(item, ensure_ascii=False))
                    # With a ChatGPT login Codex searches the web through its own connector.
                    if item.get("server") == "codex_apps" and "search" in str(item.get("tool", "")):
                        t.web_calls += 1
                elif kind == "web_search":
                    t.network_inputs.append(json.dumps(item, ensure_ascii=False))
                    t.web_calls += 1
                else:
                    t.tool_inputs.append(json.dumps(item, ensure_ascii=False))
            elif ev.get("type") in ("error", "turn.failed"):
                t.errors.append(json.dumps(ev, ensure_ascii=False))
        final = out / "final.txt"
        t.final = final.read_text() if final.exists() else (t.shown[-1] if t.shown else "")
        return t


class Claude(Agent):
    name, binary, api = "claude", "claude", "anthropic"

    def command(self, ws, port, prompt, real_home, out, model, extra, mcp=None):
        # Claude Code's own sandbox, as Codex runs in workspace-write: commands run without a prompt
        # but cannot write outside the workspace or reach the network; file edits are accepted.
        sandbox = json.dumps({"sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True,
                                          "allowUnsandboxedCommands": False}})
        argv = [shutil.which(self.binary), "-p", "--output-format", "stream-json", "--verbose",
                "--setting-sources", "project,local", "--strict-mcp-config", "--settings", sandbox,
                "--permission-mode", "acceptEdits",
                *(["--mcp-config", json.dumps({"mcpServers": {mcp["name"]: {
                    "command": mcp["command"], "args": mcp["args"], "env": mcp["env"]}}}),
                   "--allowedTools", f"mcp__{mcp['name']}"] if mcp else []),
                *(["--model", model] if model else []), *extra, "--", prompt]
        return argv, {**_clean_env(real_home), "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}"}

    def transcript(self, stdout, out):
        t = Transcript()
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "assistant":
                for b in (ev.get("message") or {}).get("content") or []:
                    if b.get("type") == "text":
                        t.shown.append(b.get("text", ""))
                    elif b.get("type") == "thinking":
                        t.shown.append(b.get("thinking", ""))
                    elif b.get("type") == "tool_use":
                        inp = b.get("input") or {}
                        if b.get("name") == "Bash":
                            t.commands.append(str(inp.get("command", "")))
                        elif str(b.get("name", "")).startswith("mcp__"):
                            t.mcp_inputs.append(json.dumps(inp, ensure_ascii=False))
                        elif b.get("name") in ("WebSearch", "WebFetch"):
                            t.network_inputs.append(json.dumps(inp, ensure_ascii=False))
                            t.web_calls += 1
                        else:
                            t.tool_inputs.append(json.dumps(inp, ensure_ascii=False))
            elif ev.get("type") == "user":
                for b in (ev.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        c = b.get("content")
                        t.shown.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
            elif ev.get("type") == "result":
                t.final = str(ev.get("result", ""))
                if ev.get("is_error"):
                    t.errors.append(json.dumps(ev, ensure_ascii=False)[:2000])
        return t


class Antigravity(Agent):
    """The Antigravity CLI, signed in with Google: Code Assist on daily-cloudcode-pa, which
    CLOUD_CODE_URL moves to keepmum's /antigravity. Live only: the scripted model does not speak
    Gemini yet. No MCP: agy takes MCP servers only from its global config, never for one session.
    Its conversations are kept in ~/.gemini/antigravity-cli, as any agy run's are."""

    name, binary, api = "agy", "agy", "antigravity"
    # search_web asks Code Assist (generateContent, requestType web_search): back through keepmum.
    ROUTED = frozenset({"search_web"})
    NETWORK = frozenset({"read_url_content"})  # and browser_*: fetched from this Mac
    modes = ("live",)
    mcp = False

    def command(self, ws, port, prompt, real_home, out, model, extra, mcp=None):
        # Headless agy denies every command no rule allows. This rule holds for this session only
        # (--settings is never saved); --sandbox restricts the terminal, as Claude Code's does.
        settings = json.dumps({"permissions": {"allow": ["command(*)"]}})
        argv = [shutil.which(self.binary), "-p", prompt, "--output-format", "stream-json", "--mode", "accept-edits",
                "--sandbox", "--settings", settings, *(["--model", model] if model else []), *extra]
        env = {k: v for k, v in _clean_env(real_home).items() if not k.startswith("AGY_LLM_GATEWAY")}
        return argv, {**env, "CLOUD_CODE_URL": f"http://127.0.0.1:{port}/antigravity"}

    def transcript(self, stdout, out):
        t = Transcript()
        said: dict[int, list[str]] = {}
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            step = ev.get("step_update") or {}
            if step.get("step_type") == "agent_response" and step.get("text_delta"):
                said.setdefault(step.get("step_index", 0), []).append(step["text_delta"])
            elif step.get("step_type") == "tool" and step.get("state") in ("DONE", "ERROR"):
                info = step.get("tool_info") or {}
                params = info.get("parameters") or {}
                if info.get("name") == "run_command":
                    t.commands.append(str(params.get("CommandLine", "")))
                elif str(info.get("name", "")).startswith("mcp"):
                    t.mcp_inputs.append(json.dumps(params, ensure_ascii=False))
                elif info.get("name") in self.ROUTED:
                    t.web_calls += 1  # the search is a Code Assist call, scanned by keepmum like any other
                elif info.get("name") in self.NETWORK or str(info.get("name", "")).startswith("browser_"):
                    t.network_inputs.append(json.dumps({"tool": info.get("name"), **params}, ensure_ascii=False))
                    t.web_calls += 1
                else:
                    t.tool_inputs.append(json.dumps({"tool": info.get("name"), **params}, ensure_ascii=False))
                if info.get("output"):
                    t.shown.append(str(info["output"]))
                if info.get("error"):
                    t.shown.append(json.dumps(info["error"], ensure_ascii=False))
            elif ev.get("event") == "result":
                result = ev.get("result") or {}
                t.final = str(result.get("response", ""))
                if result.get("status") != "SUCCESS":
                    t.errors.append(json.dumps(result, ensure_ascii=False)[:2000])
        t.shown += ["".join(parts) for _, parts in sorted(said.items())]
        return t


AGENTS: dict[str, Agent] = {a.name: a for a in (Codex(), Claude(), Antigravity())}
