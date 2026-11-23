---
title: Use with your agents
description: Start Claude Code, Codex, the Antigravity CLI or Gemini CLI through keepmum, in the terminal or in VS Code.
---

## In the terminal

```sh
keepmum claude          # starts Claude Code with every request scanned; arguments pass through to `claude`
keepmum codex           # the same for Codex (ChatGPT login or API key); arguments pass through to `codex`
keepmum agy             # the same for the Antigravity CLI (signed in with Google, or through a gateway); arguments pass through to `agy`
keepmum gemini          # the same for Gemini CLI (an API key, or a Code Assist Standard or Enterprise sign-in); arguments pass through to `gemini`
```

To send every session of Claude Code, Codex, pi and opencode through keepmum without starting them this way, see [Background proxy](/docs/start/background/).

### Codex

`keepmum codex` adds a `-c` override for that run only: Codex's own OpenAI provider pointed at the proxy (`openai_base_url`), with your existing Codex login. Your Codex config file is not changed.

### Gemini CLI and the Antigravity CLI

Since June 18, 2026, Gemini CLI no longer serves personal Google sign-ins (Google AI Pro, Ultra, and free Gemini Code Assist); Google moved them to the Antigravity CLI ([Google's announcement](https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/)). With a personal Google account, use `keepmum agy`. Gemini CLI still works with an API key, and with Sign in with Google under a Code Assist Standard or Enterprise licence, and `keepmum gemini` covers both. `agy plugin import gemini` turns your Gemini CLI extensions into Antigravity plugins and moves their MCP servers into `~/.gemini/config/mcp_config.json`; keepmum's [extension watch](/docs/watch/extensions/) lists what it brings.

`keepmum gemini` points Gemini CLI at keepmum through its environment, which wins over every `.env` file Gemini CLI reads. It covers both sign-ins: an API key (the Gemini API) and Sign in with Google (Code Assist, whose setup and metrics calls carry no conversation text and pass through; any other Code Assist call is blocked). Vertex AI is refused, not sent. If you already point Gemini CLI at a gateway (`GOOGLE_GEMINI_BASE_URL` or `CODE_ASSIST_ENDPOINT`), keepmum forwards to it. There is no `keepmum protect` for Gemini CLI: it reads its address only from the environment or the first `.env` file it finds walking up from your project, so any project's own `.env` would override a setting keepmum made.

`keepmum agy` points the Antigravity CLI at keepmum through `CLOUD_CODE_URL`, which carries every Code Assist call it makes, the model's included. Its setup calls (a project or conversation id, no text) pass through; any other call is blocked. If you already set `CLOUD_CODE_URL`, keepmum forwards to it. With a company gateway (`AGY_LLM_GATEWAY_URL`), agy sends its model calls there in the Gemini API's format: keepmum takes the variable's place and forwards to your gateway, scanned. It refuses to start when that gateway would be reached some other way, which keepmum could not scan or stand in front of (`AGY_LLM_GATEWAY_WIRE_PROTOCOL`, whose `openai` is Chat Completions; `AGY_LLM_GATEWAY_PROXY_URL`; `AGY_LLM_GATEWAY_CLIENT_CERT`; or `CLOUD_CODE_URL` set as well), and with `--remote-agent`, which runs the conversation on Google's machines. agy's commands (`run_command`) get values quoted for the shell like any agent's, and its browser and URL tools keep placeholders. Its MCP calls go through `call_mcp_tool`, which names the server in its arguments: keepmum decides each one as that server's tool, so a server gets back the values it returned, as with any agent.

## In VS Code (Claude Code extension)

Add this to your VS Code settings (JSON), then reload the window:

```json
"claudeCode.claudeProcessWrapper": "/Users/<you>/.local/bin/keepmum-claude-wrapper"
```

Each conversation then starts through keepmum. If `keepmum serve` is running, every conversation shares it (one model in memory); otherwise each conversation starts its own proxy.
