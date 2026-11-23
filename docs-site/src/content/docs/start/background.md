---
title: Background proxy
description: Run keepmum in the background and send every session of Claude Code, Codex, pi and opencode through it.
---

```sh
keepmum serve           # http://127.0.0.1:7788
keepmum protect on      # Claude Code, Codex, pi and opencode now send every session to it; `keepmum protect off` undoes it
```

`keepmum protect` changes, for each tool that is installed (`--client claude|codex|pi|opencode` picks one):

| Tool | File | Change |
| --- | --- | --- |
| Claude Code | `~/.claude/settings.json` | `env.ANTHROPIC_BASE_URL` |
| Codex | `~/.codex/config.toml` | a marked `openai_base_url`, or the `base_url` of the provider it selects (a gateway) |
| pi | `~/.pi/agent/models.json` | `baseUrl` for the `anthropic`, `openai` and `openai-codex` providers |
| opencode | `~/.config/opencode/opencode.json` | `provider.<id>.options.baseURL` for `anthropic` and `openai` |

Each file is backed up next to itself first, and `off` removes only what `on` added.

## When the change takes effect

- **Claude Code** sessions switch at once, both ways, running ones included: a running session takes a new address when the file is saved but keeps the last one if the line is simply removed, so `off` first sets the address keepmum was forwarding to (Anthropic, or your gateway), and takes that line out 15 seconds later, once running sessions have taken it. `keepmum include` puts an [excluded project](/docs/masking/exclude/) back the same way, so Claude Code already open there goes through keepmum at once.
- **Codex** reads its config as each conversation starts or reopens, so nothing needs restarting: every new conversation goes through keepmum, in a terminal, an editor or the Codex app already running, and so does an older one when it is reopened. A conversation open at the moment you switch keeps its route until it is closed; keepmum notes those, and `keepmum protect` (and the Mac app) says how many are still open.
- **pi and opencode** pick the change up when they next start. keepmum edits opencode's `opencode.json` only while it is plain JSON (no comments), and a base URL set in `opencode.jsonc` wins and is reported, not changed.

While protection is on, `keepmum serve` must keep running, or the protected agents cannot connect: the Mac app runs it at login. Claude Code turns off Remote Control when its base URL is not api.anthropic.com.
