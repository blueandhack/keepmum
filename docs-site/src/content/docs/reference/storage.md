---
title: What keepmum stores, and where
description: Every file keepmum writes, what is kept only in memory, and the only network calls it makes.
sidebar:
  label: Storage and network
---

| What | Where |
| --- | --- |
| Per-install key for placeholders | `~/.config/keepmum/salt` (0600), shared by the app, the CLI and the VS Code wrapper |
| Stats token for the Mac app | `~/.config/keepmum/stats-token` (0600) |
| Logs: paths, types, counts and timings, never values | `~/.config/keepmum/keepmum.log` (0600), 5 MB, then `keepmum.log.1` |
| Running proxy's port | `~/.config/keepmum/runtime.json` (0600) |
| Request history for the Activity page: time, endpoint, outcome, counts by type, tool calls' decisions; never values | `~/.config/keepmum/history.jsonl` (0600), last 20,000 requests |
| Audit log: requests (counts, rules, restore decisions per tool), protection changes, starts and stops; never values | `~/.config/keepmum/audit.jsonl` (0600), with its head in `audit.head.json`; `[audit] keep_days`, 90 by default, and at most 1 GB (about 2.8 million requests), the oldest going first |
| Network tools seen: server and tool names, a hash of each tool's definition and the first sentence of its description, kept-call counts per day and type; never values | `~/.config/keepmum/tools.json` (0600), counts for the last 30 days |
| Agent extensions you already have: agent, kind, name, and when first seen; never settings or instruction text | `~/.config/keepmum/extensions.json` (0600) |
| A copy of an agent's settings file before keepmum takes an entry out of it | `~/.config/keepmum/backups/` (0600), one file per removal |
| The optional checks' models, only after `keepmum guard setup` | `~/.local/share/keepmum/guard/` (`prompt-injection-guard-small-v2.3/`, `content-safety-guard-small/`, `qwen3guard/`); `keepmum guard remove injection\|harm` deletes them |
| Check hits: check, tool, score, length and categories; never the text | Process memory (the stats feed, the last 200) and the audit log |
| The Logs page's rows: per check hit the above and what came of it (answered on this Mac, handed back, your review's answers, why a rewrite did not pass); per masked value its type, rule, who found it and length. Never text, values or previews | `~/.config/keepmum/logs.jsonl` (0600), for `[log] keep_days` (30 by default; `keepmum log keep 7\|off`); `off` deletes it and keeps the Logs page in memory only |
| Results the checks flagged, so a restart does not warn about a resent conversation again: per hit an HMAC of the text under the install key, the check, model, tool, score, length and categories; never the text | `~/.config/keepmum/flagged.jsonl` (0600), for `[log] keep_days`, the newest 4,096 per check; `off` deletes it and keeps them in memory only |
| The Privacy Filter's results, so a restart does not send a resumed conversation to the model again: per text an HMAC of it under a key derived from the install key, and where the model found something (start, end, type, score); never the text | `~/Library/Caches/keepmum/model-spans.json` (0600, its folder 0700 and out of Time Machine), the newest 4,096 texts; another model or pf-serve build starts it over, and deleting it costs only the time to read those texts again |
| The Logs page's check details: the flagged text (up to 4,000 characters) and what the user had typed, every masked value and placeholder in them shown as its type | Process memory only, with the last 200 hits; `[log] context = false` turns it off and forgets what was kept |
| Masked values, and which tool servers returned each | Process memory only, the 10,000 used most recently, cleared on exit |
| The Logs page: per masked value its type, rule and length, and the first and last character of values of 8 or more characters | Previews: process memory only (the last 2,000), cleared on exit; `[log] previews = false` keeps types and rules only. The rows without previews: as above |
| The Logs and Activity pages' original values, with `[log] values = true` (off by default) | Process memory only, with the findings; shown in the app only after Touch ID or the login password; `[log] values = false` forgets what was kept |
| The Logs page's context: up to 80 characters each side of a masked value, with every masked value and placeholder in it shown as its type (`‹SECRET›`), as the provider received it | Process memory only, with the findings; `[log] context = false` turns it off and forgets what was kept |

## Network calls

keepmum makes no network calls except the request it forwards, `keepmum setup`'s one model download, and `keepmum guard setup`'s downloads if you run them. One more: with the injection check on and its model from an earlier keepmum version, the background service downloads the current one (164 MB, from the same Hugging Face org, pinned and checked) when it starts.

There is no telemetry. The Mac app can check for updates; it asks on its first launch and checks only if you say yes (Settings › Updates). Each check downloads the update feed from keepmum.app (served through Cloudflare), which sees your IP address and keepmum's version; checks and updates are counted per day and version, and no addresses are kept ([`worker/`](https://github.com/blueandhack/keepmum/tree/main/worker)). Settings › Updates › Count this Mac turns the counting off; [keepmum.app/privacy](https://keepmum.app/privacy) has the details.
