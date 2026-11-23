---
title: Configuration
description: Every key in ~/.config/keepmum/config.toml.
---

Optional, in `~/.config/keepmum/config.toml`. Unknown keys and bad values are errors. Each key's comment names the command that changes it, so you rarely need to edit the file by hand.

```toml
[detect]
pii = false            # also mask names, emails, phones, addresses, account numbers (`keepmum pii on|off`)
model = true           # the local model after the rules; false: the rules alone (`keepmum model on|off`)
observe = false        # count what would be masked, but send requests UNMASKED (`keepmum observe on|off`)
pass = []              # types never masked, e.g. ["PERSON", "DATE"] (`keepmum types pass person`)

[rules]                # your own patterns, always masked; with a group, only group 1 (`keepmum rules mask`)
mask = [{ name = "Employee ID", pattern = "EMP-\\d{6}", type = "ACCOUNT" }]
off = []               # built-in rules turned off, e.g. ["key-in-prose"] (`keepmum rules off`, `keepmum rules builtin`)

[allowlist]            # added to the built-in list (documented example keys, test cards)
strings = []
patterns = []

[restore]              # per tool: "restore" every value, "keep" placeholders, "own" (what that server returned),
default = "restore"    #   or a list of types: its own values and these (`keepmum tools`); never SECRET
WebFetch = "keep"
WebSearch = "keep"
"mcp__*" = "own"
"mcp__claude_ai_Gmail__*" = ["EMAIL", "PERSON"]

[server]
port = 7788            # for `keepmum serve`

[destinations]         # hosts a shell command may also send a rule's tokens to (a self-hosted GitLab): `keepmum destinations add gitlab-token gitlab.corp.example --yes`
gitlab-token = ["gitlab.corp.example"]

[gateways]             # gateways keepmum forwards to, each at http://127.0.0.1:7788/gateway/<name> (`keepmum protect on` adds them)
local-3425 = "http://127.0.0.1:3425"

[log]
level = "info"         # logs contain types and counts, never values
previews = true        # the app's Logs page shows the first and last character of masked values (memory only; `keepmum log previews on|off`)
context = true         # and the text around each, and what the checks flagged, every masked value shown as its type (memory only; `keepmum log context on|off`)
values = false         # the app's Logs and Activity pages may show each masked value as it was, behind Show values and Touch ID (memory only; `keepmum log values on|off`)
keep_days = 30         # the Logs page's rows, without text or previews, kept on disk across restarts this long; 0: memory only (`keepmum log keep`)

[audit]
keep_days = 90         # how long the audit log keeps entries (`keepmum audit keep 365`)

[guard]
injection = "off"      # check tool results for prompt injection: "warn" or "block" (`keepmum guard setup injection` first)
injection_confirm = true  # report an injection hit only if a second model agrees: far fewer false alarms (`keepmum guard confirm`)
harm = "off"           # check what you type for harmful requests: "warn" or "block" (`keepmum guard setup harm` first)
harm_model = "small"   # or Qwen3Guard "0.6b" or "4b"
commands = "off"       # check the shell commands in the model's replies: "warn" or "block" (no setup)
# injection or harm = "local": a flagged request goes to the first of these that answers (on this Mac only);
# checks: only for those (default both); api: "auto" (default), "native" or "chat" (`keepmum guard local`)
local_servers = [{ url = "http://localhost:11434", model = "qwen3:8b" },
                 { url = "http://localhost:1234", model = "qwen/qwen3-14b", checks = ["harm"] }]
```

## Where each section is explained

- `[detect]`, `[rules]`, `[allowlist]`: [Choose what gets masked](/docs/masking/what-gets-masked/), [Rules only](/docs/masking/rules-only/), [Observe only](/docs/masking/observe/)
- `[restore]`: [Tools that reach the network](/docs/masking/network-tools/)
- `[destinations]`: shell commands, in [Limitations](/docs/about/limitations/)
- `[gateways]`: [In front of another gateway](/docs/start/gateways/)
- `[log]`, `[audit]`: [What keepmum stores, and where](/docs/reference/storage/), [Audit log](/docs/watch/audit/)
- `[guard]`: [Optional checks](/docs/checks/), [Sensitivity](/docs/checks/sensitivity/), [Local mode](/docs/checks/local/)
