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


# Live runs sign in as the user does (their logins), or with API keys (`--auth keys`), read from the
# run's environment under these names, as `op run --env-file e2e/keys.env` puts them there (copy
# e2e/keys.env.example). Codex, opencode and pi use the OpenAI key, Claude Code the Anthropic one. Each
# agent gets only the key it uses, under the name it reads; no key is written to disk.
AUTH = {"mode": "login", "cheap": False, "provider": None}
KEY_VARS = {"openai": "KEEPMUM_OPENAI_API_KEY", "anthropic": "KEEPMUM_CLAUDE_API_KEY",
            "deepseek": "KEEPMUM_DEEPSEEK_API_KEY"}
# `--provider deepseek`: DeepSeek through keepmum, as a gateway in front of its Anthropic-format
# endpoint (keepmum scans no Chat Completions, DeepSeek's other format, and refuses it). Claude
# Code, pi and opencode speak that format; Codex only OpenAI's Responses API, agy only Google's.
DEEPSEEK_MODELS = ("deepseek-v4-pro", "deepseek-flash")  # (default, --cheap)
# Provider keys never reach an agent unless its run sets one: a key in the user's shell (or a
# template value) would decide which account an agent uses, and its shell commands could read it.
_PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", *KEY_VARS.values())


def keys() -> bool:
    return AUTH["mode"] == "keys"


def deepseek() -> bool:
    return keys() and AUTH.get("provider") == "deepseek"


def deepseek_model(model: str | None) -> str:
    return model or DEEPSEEK_MODELS[AUTH["cheap"]]


def deepseek_url(port: int) -> str:
    """keepmum's route for the DeepSeek gateway (run.py), at DeepSeek's Anthropic-format base."""
    return f"http://127.0.0.1:{port}/gateway/deepseek/anthropic"


def api_key(provider: str) -> str:
    value = os.environ.get(KEY_VARS[provider])
    if not value:
        raise RuntimeError(f"--auth keys: {KEY_VARS[provider]} is not set (run under `op run --env-file e2e/keys.env --`)")
    return value


def _clean_env(real_home: str) -> dict[str, str]:
    """The environment of the user's terminal: none of the variables a host agent (this one) set."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE_CODE_", "CLAUDECODE", "CLAUDE_AGENT_SDK", "CLAUDE_PID", "CLAUDE_EFFORT"))
           and k not in ("ANTHROPIC_BASE_URL", "KEEPMUM_PF_SERVE", "HF_HOME", *_PROVIDER_KEYS)}
    env["HOME"] = real_home
    return env


class Agent:
    name = ""
    binary = ""
    api = ""  # the provider route keepmum serves it on: anthropic, chatgpt, openai, antigravity
    modes = ("scripted", "live")  # scripted needs the scripted model to speak its provider's API
    mcp_modes = ("scripted", "live")  # where it can be given the stub MCP server for one run
    key_auth = True  # can run live on an OpenAI or Anthropic API key (--auth keys)
    deepseek = False  # can run on DeepSeek through keepmum (--provider deepseek): speaks the Messages API

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
                extra: list[str], mcp: dict | None = None, mode: str = "live") -> tuple[list[str], dict[str, str]]:
        """`mcp`: one stdio MCP server to give the agent, {name, command, args, env}."""
        raise NotImplementedError

    def transcript(self, stdout: str, out: Path) -> Transcript:
        raise NotImplementedError


class Codex(Agent):
    name, binary = "codex", "codex"

    @property
    def api(self) -> str:
        from keepmum.clients import codex_auth_mode

        return "openai" if keys() or codex_auth_mode() == "apikey" else "chatgpt"

    KEY_MODELS = ("gpt-6.1-sol", "gpt-6-luna")  # --auth keys: (default, --cheap)

    def command(self, ws, port, prompt, real_home, out, model, extra, mcp=None, mode="live"):
        from keepmum.clients import codex_args

        servers = []
        if mcp:
            key = f"mcp_servers.{mcp['name']}"
            env = ",".join(f"{k}={json.dumps(v)}" for k, v in mcp["env"].items())
            servers = ["-c", f"{key}.command={json.dumps(mcp['command'])}", "-c", f"{key}.args={json.dumps(mcp['args'])}",
                       "-c", f"{key}.env={{{env}}}", "-c", f'{key}.default_tools_approval_mode="approve"']
        env, provider = _clean_env(real_home), None
        if mode == "live" and keys():
            # keepmum's provider as `keepmum protect` writes it for an API key, the key read from
            # the environment (env_key) so it is never written to Codex's auth file.
            provider = {"name": "keepmum", "base_url": f"http://127.0.0.1:{port}/openai/v1", "wire_api": "responses",
                        "env_key": "OPENAI_API_KEY", "supports_websockets": False,
                        "http_headers": {"x-keepmum-client": "codex"}}
            env["OPENAI_API_KEY"] = api_key("openai")
            model = model or self.KEY_MODELS[AUTH["cheap"]]
        argv = [shutil.which(self.binary), "exec", "--json", "--skip-git-repo-check", "--ephemeral",
                "--ignore-user-config", "-s", "workspace-write", "-C", str(ws), "-o", str(out / "final.txt"),
                *codex_args(port, provider), *servers, *(["-m", model] if model else []), *extra, prompt]
        return argv, env

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

    deepseek = True
    KEY_MODELS = (None, "claude-haiku-5-5")  # --auth keys: (default: Claude Code's own, --cheap)

    def command(self, ws, port, prompt, real_home, out, model, extra, mcp=None, mode="live"):
        if mode == "live" and keys():
            model = deepseek_model(model) if deepseek() else model or self.KEY_MODELS[AUTH["cheap"]]
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
        env = {**_clean_env(real_home), "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}"}
        if mode == "scripted":  # the scripted model takes any key; no login is used
            env["ANTHROPIC_API_KEY"] = DUMMY_ANTHROPIC_KEY
        elif keys():
            # A config folder of the run's own, so the user's Claude login is not there to prefer.
            config = out / "claude-config"
            config.mkdir(exist_ok=True)
            env.update(ANTHROPIC_API_KEY=api_key("anthropic"), CLAUDE_CONFIG_DIR=str(config))
            if deepseek():
                # Every model Claude Code picks on its own (titles, subagents) is DeepSeek's too.
                ds = model  # set above from DEEPSEEK_MODELS
                env.update(ANTHROPIC_BASE_URL=deepseek_url(port), ANTHROPIC_API_KEY=api_key("deepseek"),
                           ANTHROPIC_DEFAULT_HAIKU_MODEL=ds, ANTHROPIC_DEFAULT_SONNET_MODEL=ds,
                           ANTHROPIC_DEFAULT_OPUS_MODEL=ds, ANTHROPIC_SMALL_FAST_MODEL=ds)
        return argv, env

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
    """The Antigravity CLI. Live: signed in with Google, Code Assist on daily-cloudcode-pa, which
    CLOUD_CODE_URL moves to keepmum's /antigravity; its conversations are kept in the user's
    ~/.gemini/antigravity-cli. Scripted: no login at all, its gateway mode (AGY_LLM_GATEWAY_URL, a
    dummy key) on keepmum's /gemini, in a HOME of its own. Both are set as `keepmum agy` sets them.
    MCP scripted only: agy takes MCP servers only from ~/.gemini/config, which only the scripted
    run's own HOME lets the harness write."""

    name, binary, api = "agy", "agy", "antigravity"
    MCP = frozenset({"call_mcp_tool", "list_resources", "read_resource"})  # the server is in the arguments
    # search_web asks Code Assist (generateContent, requestType web_search): back through keepmum.
    ROUTED = frozenset({"search_web"})
    # Fetched or done from this Mac: keepmum keeps placeholders in them (restore.DEFAULT_POLICY).
    NETWORK = frozenset({"read_url_content", "open_browser_url", "read_browser_page", "click_browser_pixel",
                         "execute_browser_javascript", "capture_browser_console_logs", "capture_browser_screenshot",
                         "list_browser_pages"})  # and browser_*
    mcp_modes = ("scripted",)

    key_auth = False  # it signs in with Google: --auth keys covers OpenAI and Anthropic
    def command(self, ws, port, prompt, real_home, out, model, extra, mcp=None, mode="live"):
        # Headless agy denies every command no rule allows. This rule holds for this session only
        # (--settings is never saved). Live, --sandbox restricts the terminal, as Claude Code's does.
        # Scripted, the commands are the case's own, and agy's sandbox, in a HOME of its own, would
        # refuse every write to the workspace.
        settings = json.dumps({"permissions": {"allow": ["command(*)", *(["mcp(*)"] if mcp else [])]}})
        argv = [shutil.which(self.binary), "-p", prompt, "--output-format", "stream-json", "--mode", "accept-edits",
                *(["--sandbox"] if mode == "live" else []), "--settings", settings,
                *(["--model", model] if model else []), *extra]
        from keepmum.launch import antigravity_env  # as `keepmum agy` sets it

        env = {k: v for k, v in _clean_env(real_home).items() if not k.startswith("AGY_LLM_GATEWAY")}
        if mode == "scripted":
            home = out / "agy-home"
            home.mkdir(exist_ok=True)
            env |= {"HOME": str(home), "AGY_LLM_GATEWAY_URL": "https://gateway.e2e.invalid",
                    "AGY_LLM_GATEWAY_API_KEY": "e2e-dummy-key"}
            if mcp:
                config = home / ".gemini" / "config"
                config.mkdir(parents=True, exist_ok=True)
                (config / "mcp_config.json").write_text(json.dumps({"mcpServers": {mcp["name"]: {
                    "command": mcp["command"], "args": mcp["args"], "env": mcp["env"], "disabled": False}}}))
        return argv, antigravity_env(port, env)

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
                elif info.get("name") in self.MCP or str(info.get("name", "")).startswith("mcp"):
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


DUMMY_ANTHROPIC_KEY = "sk-ant-api03-e2e-dummy-not-a-key-" + "0" * 60 + "AA"


class Pi(Agent):
    """pi, pointed at keepmum as `keepmum protect` points it: a models.json giving its providers
    keepmum's base URLs (clients.pi_providers), in an agent folder of the run's own
    (PI_CODING_AGENT_DIR), so the user's pi setup stays out. Scripted, it speaks Anthropic with a
    dummy key. Live, it uses the user's own login (/login in pi; Claude here): the run's folder
    links auth.json to the user's, which pi rewrites in place under a lock, so a refreshed token
    lands in the user's file; a copy could rotate the refresh token away from it."""

    name, binary, api = "pi", "pi", "anthropic"
    deepseek = True
    mcp_modes = ()
    MODEL = "claude-opus-4-5"
    # The login the live run uses: anthropic (a Claude login) or openai-codex (ChatGPT).
    LIVE_PROVIDER = os.environ.get("KEEPMUM_E2E_PI_PROVIDER", "anthropic")
    LIVE_MODEL = {"anthropic": "claude-sonnet-5-5", "openai-codex": "gpt-5.5"}.get(LIVE_PROVIDER)
    KEY_MODELS = ("gpt-6.1-sol", "gpt-6-luna")  # --auth keys, OpenAI: (default, --cheap)

    def command(self, ws, port, prompt, real_home, out, model, extra, mcp=None, mode="live"):
        from keepmum.clients import pi_providers

        live = mode == "live"
        agent = out / "pi-agent"
        agent.mkdir(exist_ok=True)
        providers = {name: {"baseUrl": url, "headers": {"x-keepmum-client": "pi"}} for name, url in pi_providers(port).items()}
        env = {**_clean_env(real_home), "PI_CODING_AGENT_DIR": str(agent), "PI_OFFLINE": "1"}
        provider = "anthropic"
        if not live:
            env["ANTHROPIC_API_KEY"] = DUMMY_ANTHROPIC_KEY
            model = model or self.MODEL
        elif deepseek():
            provider, model = "keepmum-deepseek", deepseek_model(model)
            providers["keepmum-deepseek"] = {
                "baseUrl": deepseek_url(port), "api": "anthropic-messages", "apiKey": "$DEEPSEEK_API_KEY",
                "headers": {"x-keepmum-client": "pi"},
                "models": [{"id": model, "reasoning": True, "contextWindow": 1_048_576, "maxTokens": 32_768,
                            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}
            env["DEEPSEEK_API_KEY"] = api_key("deepseek")
        elif keys():
            env["OPENAI_API_KEY"] = api_key("openai")
            provider, model = "openai", model or self.KEY_MODELS[AUTH["cheap"]]
        else:
            real = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path(real_home) / ".pi" / "agent") / "auth.json"
            (agent / "auth.json").symlink_to(real)
            provider, model = self.LIVE_PROVIDER, model or self.LIVE_MODEL
        (agent / "models.json").write_text(json.dumps({"providers": providers}))
        argv = [shutil.which(self.binary), "-p", "--mode", "json", "--no-session", "--no-extensions", "--no-skills",
                "--no-prompt-templates", "--no-themes", "--offline", "--provider", provider, "--model", model,
                *extra, prompt]
        return argv, env

    def transcript(self, stdout, out):
        t = Transcript()
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("type")
            if kind == "tool_execution_start":
                args = ev.get("args") or {}
                if ev.get("toolName") == "bash":
                    t.commands.append(str(args.get("command", "")))
                else:
                    t.tool_inputs.append(json.dumps({"tool": ev.get("toolName"), **args}, ensure_ascii=False))
            elif kind == "tool_execution_end":
                for c in ((ev.get("result") or {}).get("content") or []):
                    if isinstance(c, dict) and c.get("type") == "text":
                        t.shown.append(c.get("text", ""))
            elif kind == "message_end" and (ev.get("message") or {}).get("role") == "assistant":
                text = "".join(c.get("text", "") for c in ev["message"].get("content") or []
                               if isinstance(c, dict) and c.get("type") == "text")
                if text:
                    t.shown.append(text)
                    t.final = text
                if ev["message"].get("stopReason") == "error" or ev["message"].get("errorMessage"):
                    t.errors.append(json.dumps(ev["message"], ensure_ascii=False)[:2000])
        return t


class OpenCode(Agent):
    """opencode, pointed at keepmum as `keepmum protect` points it: an opencode.json giving its
    providers keepmum's base URLs (clients.opencode_providers), in config, cache and state folders of
    the run's own (XDG_*), so the user's setup stays out. Scripted, its data folder is the run's own
    too and it speaks Anthropic with a dummy key. Live, it uses the user's data folder, where its
    ChatGPT login is stored (a copy could rotate the refresh token away), so each run is kept in the
    user's session list; OPENAI_API_KEY is left out, so the login, not a key, is what is tested."""

    name, binary, api = "opencode", "opencode", "anthropic"
    NETWORK = frozenset({"webfetch", "websearch"})  # sent from this Mac, or by the provider
    deepseek = True
    MODEL = "anthropic/claude-opus-4-5"
    LIVE_MODEL = "openai/gpt-6.1-sol"
    KEY_MODELS = ("openai/gpt-6.1-sol", "openai/gpt-6-luna")  # --auth keys, OpenAI: (default, --cheap)

    def command(self, ws, port, prompt, real_home, out, model, extra, mcp=None, mode="live"):
        from keepmum.clients import opencode_providers

        live = mode == "live"
        login = live and not keys()  # the user's stored login: their data folder
        xdg = {k: out / f"opencode-{k.split('_')[1].lower()}" for k in
               ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")
               if not (login and k == "XDG_DATA_HOME")}
        for d in xdg.values():
            d.mkdir(exist_ok=True)
        config = xdg["XDG_CONFIG_HOME"] / "opencode"
        config.mkdir(exist_ok=True)
        (config / "opencode.json").write_text(json.dumps({
            "$schema": "https://opencode.ai/config.json", "autoupdate": False, "share": "disabled",
            # --auto approves what is not denied, but a web search still asks (headless, it is cancelled).
            "permission": {"websearch": "allow", "webfetch": "allow"},
            "provider": {**{name: {"options": {"baseURL": url, "headers": {"x-keepmum-client": "opencode"}}}
                            for name, url in opencode_providers(port).items()},
                         **({"keepmum-deepseek": {
                             "npm": "@ai-sdk/anthropic", "name": "DeepSeek through keepmum",
                             "options": {"baseURL": deepseek_url(port) + "/v1", "apiKey": "{env:DEEPSEEK_API_KEY}",
                                         "headers": {"x-keepmum-client": "opencode"}},
                             "models": {deepseek_model(model): {"limit": {"context": 1_048_576, "output": 32_768}}}}}
                            if live and deepseek() else {})},
            **({"mcp": {mcp["name"]: {"type": "local", "command": [mcp["command"], *mcp["args"]],
                                      "environment": mcp["env"], "enabled": True}}} if mcp else {})}))
        argv = [shutil.which(self.binary), "run", "--format", "json", "--auto", "--standalone",
                "-m", ("keepmum-deepseek/" + deepseek_model(model)) if live and deepseek() else
                model or (self.KEY_MODELS[AUTH["cheap"]] if live and keys() else self.LIVE_MODEL if live
                          else self.MODEL), *extra, prompt]
        env = {**_clean_env(real_home), **{k: str(v) for k, v in xdg.items()}}
        if not live:
            env["ANTHROPIC_API_KEY"] = DUMMY_ANTHROPIC_KEY
        elif deepseek():
            env["DEEPSEEK_API_KEY"] = api_key("deepseek")
        elif keys():
            env["OPENAI_API_KEY"] = api_key("openai")
        return argv, env

    def transcript(self, stdout, out):
        t = Transcript()
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            part = ev.get("part") or {}
            if ev.get("type") == "tool_use":
                state = part.get("state") or {}
                inp = state.get("input") or {}
                tool = part.get("tool")
                if tool == "shell":
                    t.commands.append(str(inp.get("command", "")))
                elif tool == "execute":  # Code Mode: reaches MCP servers and, with fetch, the web
                    code = str(inp.get("code", ""))
                    # A script that calls a tool reaches a server, one that fetches the web (below); one
                    # that calls neither runs on this Mac only, like any local tool.
                    if "tools." in code or "tools[" in code:
                        t.mcp_inputs.append(json.dumps(inp, ensure_ascii=False))
                    elif "fetch(" not in code:
                        t.tool_inputs.append(json.dumps(inp, ensure_ascii=False))
                    if "fetch(" in code:
                        t.network_inputs.append(json.dumps(inp, ensure_ascii=False))
                        t.web_calls += 1
                elif tool in self.NETWORK:
                    t.network_inputs.append(json.dumps({"tool": tool, **inp}, ensure_ascii=False))
                    t.web_calls += 1
                else:
                    t.tool_inputs.append(json.dumps({"tool": tool, **inp}, ensure_ascii=False))
                if state.get("output"):
                    t.shown.append(str(state["output"]))
                if state.get("status") == "error":
                    t.shown.append(str(state.get("error", "")))
            elif ev.get("type") == "text" and part.get("text"):
                t.shown.append(part["text"])
                t.final = part["text"]
            elif ev.get("type") == "error":
                t.errors.append(json.dumps(ev, ensure_ascii=False)[:2000])
        return t


AGENTS: dict[str, Agent] = {a.name: a for a in (Codex(), Claude(), Antigravity(), Pi(), OpenCode())}
