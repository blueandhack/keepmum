---
title: In front of another gateway
description: Keep your gateway (LiteLLM, OpenRouter, a company proxy) and put keepmum in front of it.
---

If you already route agents through a gateway (Magpie, LiteLLM, OpenRouter, a company proxy), keep it. `keepmum protect on` puts keepmum in front of it: agent → keepmum → gateway → provider. The gateway still picks models, accounts and routes, and it only ever receives placeholders.

```sh
keepmum protect on
# Claude Code  on: goes through keepmum, then http://127.0.0.1:3425
# Codex        on: goes through keepmum, then http://127.0.0.1:3425/v1
```

Each gateway gets a route on keepmum, `http://127.0.0.1:7788/gateway/<name>`, saved under `[gateways]` in the [config](/docs/reference/configuration/) (and in the audit log). An agent that was set up for a gateway is pointed at that gateway's route, with the same path after it, so whatever the gateway serves (the Messages, Responses or Gemini API) keeps working. Anything keepmum does not scan, such as Chat Completions, is refused rather than sent. Only agents that already used the gateway go to it: an agent that went straight to its provider still does, and its key never reaches the gateway.

- **Claude Code, pi, opencode:** the base URL becomes keepmum's route. `off` puts the gateway's URL back.
- **Codex:** the provider it selects keeps its key variable, headers and auth; only its `base_url` (or Codex's own `openai_base_url`) becomes keepmum's route, the line noting the URL it replaced (`# keepmum protect (was "http://127.0.0.1:3425/v1")`), which `off` puts back. keepmum edits a provider written as a `[model_providers.<name>]` table. A provider using `wire_api = "chat"` is refused, because keepmum does not scan Chat Completions. Local models (`ollama`, `lmstudio`, `oss`) need no protection, since nothing leaves the Mac.
- **One run:** `keepmum claude`, `keepmum codex`, `keepmum gemini` and `keepmum agy` go in front of the gateway set in the environment or in Codex's config for that run only.

### DeepSeek

DeepSeek's API works the same way, as a gateway: set an agent up for DeepSeek as its own guides say, then run `keepmum protect on`. Claude Code, pi and opencode use DeepSeek's Anthropic-format endpoint (`https://api.deepseek.com/anthropic`). Codex uses DeepSeek's Responses API, set up as in [DeepSeek's Codex guide](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/) (`model_provider = "deepseek"`, `base_url = "https://api.deepseek.com/"`, `wire_api = "responses"`); keepmum points that `base_url` at its `deepseek` route. All four were tested live through keepmum on `deepseek-v4-pro` and `deepseek-flash` in November 2026. DeepSeek's Chat Completions endpoint is not scanned, so an agent set up for it is refused.

A gateway's own app may write its address back into an agent's settings, for example when you switch models in it. That agent then goes around keepmum. `keepmum doctor` reports it, and the Mac app shows it in the menu bar with a **Protect** button. Run `keepmum protect on` again to fix it. A gateway must use https unless it runs on this Mac.
