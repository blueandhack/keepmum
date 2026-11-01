<p align="center"><img src="mac/docs/logo.png" width="112" alt="keepmum logo: a lowercase m closed off by a line, like a shut mouth"></p>

<h1 align="center">keepmum</h1>

<p align="center"><b>Keep secrets and personal data on your Mac while you use Claude Code, Codex, pi or opencode.</b></p>

keepmum is a local proxy that sits between your coding agent (Claude Code, Codex, pi or opencode) and its model provider (Anthropic or OpenAI). Before a request leaves your Mac, it finds secrets (API keys, tokens, passwords, private keys, connection strings) and, if you want, personal data (names, emails, phones, addresses, account numbers), and replaces each one with a placeholder such as `KEEPMUM_SECRET_7f3a9c2e`. When the model answers, keepmum puts the real values back, so your agent can still edit your `.env` file correctly, but the provider never sees what is in it.

Detection runs entirely on your Mac. Nothing is sent anywhere except the scanned request to the provider. The name comes from "keep mum": to say nothing about a secret.

> **Status: pre-release.** keepmum works end to end, but it has not been audited. Treat it as a safety layer, not a guarantee. See [Limitations](#limitations).

![The keepmum dashboard: requests out and replies back, per coding agent](mac/docs/overview-light.png)

## How it works

```
Claude Code ─┐                            ┌──▶ Anthropic API
Codex       ─┤                            │
pi          ─┼─▶ keepmum (on your Mac) ─────┤
opencode    ─┘   detect → mask            └──▶ OpenAI API
  real values  ◀── restore ◀───────────────── placeholders only
```

1. **Detect.** Two layers, cheapest first:
   - 96 rules, plus entropy and checksum checks: provider keys (formats from gitleaks and TruffleHog), secrets in command-line flags, config files and plain sentences, and, with personal data on, emails, phone numbers, SSNs and dates of birth.
   - [OpenAI Privacy Filter](https://huggingface.co/openai/privacy-filter), an open-weight token classifier, for secrets in context and, optionally, personal data such as names and addresses. It runs in pf-serve, a small native program on [privacy-filter.cpp](https://github.com/localai-org/privacy-filter.cpp) that uses the Mac's GPU through Metal. You can turn it off and run on the rules alone (`keepmum model off`).
   - A deterministic refinement step then drops what the model gets wrong: UUIDs, hashes, request IDs, timestamps, paths, placeholders and documented example keys.
2. **Mask.** Each value becomes a placeholder derived from a secret per-install key (HMAC-SHA256). The same value always gets the same placeholder, so the provider's prompt cache keeps working. You choose what is masked: turn off a type (names, dates…), add your own patterns, and list exceptions. Values live only in memory and are cleared when keepmum exits.
3. **Restore.** Placeholders in the model's answers are swapped back on your Mac, including in streamed text and in tool inputs (file edits, shell commands). Network tools such as WebFetch, WebSearch and MCP tools keep placeholders, so a prompt-injected request cannot send a real value to the internet.
4. **Fail closed.** If scanning fails, the body is malformed or compressed in a way keepmum cannot read, a request targets an endpoint keepmum does not scan, or a client opens a WebSocket, the request is blocked and never sent raw.

## Requirements

- A Mac with Apple silicon, and macOS 14 or later
- Python 3.12+, [uv](https://docs.astral.sh/uv/), and Xcode or its command line tools (to build pf-serve)
- Claude Code, Codex, pi or opencode
- About 1.7 GB of free memory while keepmum runs (the local model), and 1.6 GB of disk for it; neither with `keepmum model off`

## Install

### The Mac app (everything included)

Download `Keepmum-<version>.dmg` from the [Releases](https://github.com/blueandhack/keepmum/releases) page (or build it: [mac/README.md](mac/README.md)), drag Keepmum into Applications, open it, and follow **Settings › Setup**: download the model, start the background service, and switch on protection for each coding agent you use (Claude Code, Codex, pi). The app bundles keepmum, its Python runtime and pf-serve; nothing else needs installing. Every session of a protected agent then goes through keepmum; for Claude Code that is the terminal, VS Code and the desktop app.

### The command line

```sh
git clone https://github.com/blueandhack/keepmum.git && cd keepmum
uv tool install --editable .                  # installs `keepmum` and `keepmum-claude-wrapper`
native/pf-serve/build.sh                      # builds pf-serve from pinned sources into ~/.local/share/keepmum/bin
keepmum setup                                 # downloads the model (1.6 GB, once), checked against its pinned SHA-256
keepmum doctor                                # every line should be ✓
keepmum selftest                              # sends a canary secret through keepmum to a local mock; fails if it leaks
```

## Use

### In the terminal

```sh
keepmum claude          # starts Claude Code with every request scanned; arguments pass through to `claude`
keepmum codex           # the same for Codex (ChatGPT login or API key); arguments pass through to `codex`
```

`keepmum codex` adds `-c` overrides for that run only: a `keepmum` model provider pointed at the proxy, with WebSockets off (keepmum refuses them, since it cannot scan them) and your existing Codex login. Your Codex config file is not changed.

### In VS Code (Claude Code extension)

Add this to your VS Code settings (JSON), then reload the window:

```json
"claudeCode.claudeProcessWrapper": "/Users/<you>/.local/bin/keepmum-claude-wrapper"
```

Each conversation then starts through keepmum. If `keepmum serve` is running, every conversation shares it (one model in memory); otherwise each conversation starts its own proxy.

### As a background proxy, for every session

```sh
keepmum serve           # http://127.0.0.1:7788
keepmum protect on      # Claude Code, Codex, pi and opencode now send every session to it; `keepmum protect off` undoes it
```

`keepmum protect` changes, for each tool that is installed (`--client claude|codex|pi|opencode` picks one):

| Tool | File | Change |
| --- | --- | --- |
| Claude Code | `~/.claude/settings.json` | `env.ANTHROPIC_BASE_URL` |
| Codex | `~/.codex/config.toml` | `model_provider = "keepmum"` and a marked `[model_providers.keepmum]` block |
| pi | `~/.pi/agent/models.json` | `baseUrl` for the `anthropic`, `openai` and `openai-codex` providers |
| opencode | `~/.config/opencode/opencode.json` | `provider.<id>.options.baseURL` for `anthropic` and `openai` |

Each file is backed up next to itself first, and `off` removes only what `on` added. A tool you have already pointed at another gateway is left alone and reported. Claude Code sessions switch at once; Codex, pi and opencode pick the change up when they next start. keepmum edits opencode's `opencode.json` only while it is plain JSON (no comments), and a base URL set in `opencode.jsonc` wins and is reported, not changed.

While protection is on, `keepmum serve` must keep running, or the protected agents cannot connect: the Mac app runs it at login. Claude Code turns off Remote Control when its base URL is not api.anthropic.com.

### Observe only

```sh
keepmum observe on       # scan and count, but send every request unmasked
keepmum observe off      # mask again
```

Observe mode shows what keepmum would catch without changing anything: requests go to the provider exactly as the agent sent them, nothing is blocked, and the dashboard says so on every page. Running proxies switch on their next request; if the config file becomes invalid, they mask.

### Rules only, without the model

```sh
keepmum model off        # scan with the rules alone
keepmum model on         # the rules, then the local model (the default)
```

The rules alone are faster and need no model download and no 1.7 GB of memory. They find secrets with a known shape (52 provider formats, private keys, JWTs), secrets under a telling name (`DB_PASSWORD=`, `--token`, "the API key is …"), long random strings, and, with personal data on, emails, phone numbers, card and bank numbers, SSNs and dates of birth. They miss what only context reveals: names, addresses, and a secret with no shape and no telling name around it. On the benchmark, secret recall is 0.99 either way; personal-data recall is 0.77 with the rules alone and 1.00 with the model ([bench/results/RULES.md](bench/results/RULES.md)). Running proxies switch on their next request, and the Mac app has the same switch in Settings › Detector.

### Choose what gets masked

```sh
keepmum types                                                        # which types are masked
keepmum types pass person date                                       # let names and dates through unmasked; `keepmum types mask person` undoes it
keepmum rules mask 'EMP-\d{6}' --name "Employee ID" --type account   # always mask your own pattern
keepmum rules allow acme-test-key                                    # never mask this exact text (or --pattern for a regex)
keepmum rules off key-in-prose --yes                                 # turn off a built-in rule that masks what it should not (`keepmum rules on` undoes it)
keepmum rules builtin                                                # every built-in rule, its type, and whether it is on
keepmum rules list                                                   # and `keepmum rules remove mask 0`
```

Letting secrets through, or turning off a rule that finds secrets, needs `--yes`. A rule turned off stops masking only what it alone finds: other rules and the local model still apply, so for one value an exception is narrower. Values masked earlier in a session follow your changes: once every detector that found a value is off, or the value has an exception, it goes as is. Your own mask rules apply even with personal data off. Running proxies pick up every change on their next request; the Mac app's Detection page edits the same settings and shows, per type, how many values were found, allowed by an exception, passed through by your settings, and masked.

### Leave a project out

```sh
keepmum exclude ~/Projects/some-repo     # Claude Code there goes straight to the API, unscanned
keepmum exclude --list
keepmum include ~/Projects/some-repo     # scan it again
```

`keepmum exclude` sets `env.ANTHROPIC_BASE_URL` to keepmum's upstream in the project's `.claude/settings.local.json` (at the git repository's root, as Claude Code reads it), which outranks `keepmum protect`. That file is never committed: keepmum refuses if git tracks it and otherwise adds it to `.git/info/exclude`. Claude Code applies it once you trust the folder; until then the project still goes through keepmum. Open sessions there switch at once when you exclude, but keep going straight to the API after `keepmum include` until they restart. In an excluded project the VS Code wrapper starts Claude Code without keepmum, and `keepmum claude` refuses. The Mac app lists exclusions in Settings.

### Audit what keepmum did

keepmum keeps a tamper-evident audit log: every request (who sent it, where it went, counts by type, which rules found what, and which tools got real values back or kept placeholders), every change to its protection (marked when it turns protection down, with the command that made it, or "config file" for an edit by hand), agents and projects taken out of its path, and each start and stop, so gaps in coverage show. Never a value, a preview or a placeholder: allowlist entries and your own patterns appear as keyed fingerprints.

```sh
keepmum audit list --weakening --since 30d   # what turned protection down, and who did it
keepmum audit verify                         # exits 1 if an entry was edited, added, removed or reordered
keepmum audit head                           # the newest <seq>:<hash>: keep it somewhere else
keepmum audit verify --anchor 4211:9be41f07c2d8a35e   # the log still holds that entry, unchanged
keepmum audit export --since 30d -o audit.jsonl       # for a review or a ticket (or --format csv)
keepmum audit verify --file audit.jsonl               # check an export
```

Each entry carries the hash of the one before, so a change inside the log breaks the chain where it happened. Someone who can write the file could still rewrite it from some point on, or cut off its end; a head kept elsewhere catches that. The Mac app's Audit page checks the chain each time it opens, against the head it saved at its last good check. Entries are kept for `[audit] keep_days` (90 days), and a shorter retention is itself recorded as weakening.

### Scan files (pre-commit)

```sh
keepmum scan path/to/file …    # prints file:line:col TYPE rule [N chars]; never prints the value; exits 1 on findings
```

## Configuration

Optional, in `~/.config/keepmum/config.toml`. Unknown keys and bad values are errors.

```toml
[detect]
pii = false            # also mask names, emails, phones, addresses, account numbers
model = true           # the local model after the rules; false: the rules alone (`keepmum model on|off`)
observe = false        # count what would be masked, but send requests UNMASKED (`keepmum observe on|off`)
pass = []              # types never masked, e.g. ["PERSON", "DATE"] (`keepmum types pass person`)

[rules]                # your own patterns, always masked; with a group, only group 1 (`keepmum rules mask`)
mask = [{ name = "Employee ID", pattern = "EMP-\\d{6}", type = "ACCOUNT" }]
off = []               # built-in rules turned off, e.g. ["key-in-prose"] (`keepmum rules off`, `keepmum rules builtin`)

[allowlist]            # added to the built-in list (documented example keys, test cards)
strings = []
patterns = []

[restore]              # per tool: "restore" real values, or "keep" placeholders
default = "restore"
WebFetch = "keep"
WebSearch = "keep"
"mcp__*" = "keep"

[server]
port = 7788            # for `keepmum serve`

[log]
level = "info"         # logs contain types and counts, never values
previews = true        # the app's Logs page shows the first and last character of masked values (memory only)
context = true         # and the text around each, with every masked value in it shown as its type (memory only)

[audit]
keep_days = 90         # how long the audit log keeps entries (`keepmum audit`)
```

## What keepmum stores, and where

| What | Where |
| --- | --- |
| Per-install key for placeholders | `~/.config/keepmum/salt` (0600), shared by the app, the CLI and the VS Code wrapper |
| Stats token for the Mac app | `~/.config/keepmum/stats-token` (0600) |
| Logs: paths, types, counts and timings, never values | `~/.config/keepmum/keepmum.log` (0600) |
| Running proxy's port | `~/.config/keepmum/runtime.json` (0600) |
| Request history for the Activity page: time, endpoint, outcome, counts by type; never values | `~/.config/keepmum/history.jsonl` (0600), last 20,000 requests |
| Audit log: requests (counts, rules, restore decisions per tool), protection changes, starts and stops; never values | `~/.config/keepmum/audit.jsonl` (0600), with its head in `audit.head.json`; `[audit] keep_days`, 90 by default |
| Masked values | Process memory only, cleared on exit |
| The Logs page: per masked value its type, rule and length, and the first and last character of values of 8 or more characters | Process memory only (the last 2,000), cleared on exit; `[log] previews = false` keeps types and rules only |
| The Logs page's context: up to 80 characters each side of a masked value, with every masked value and placeholder in it shown as its type (`‹SECRET›`), as the provider received it | Process memory only, with the findings; `[log] context = false` turns it off and forgets what was kept |

keepmum makes no network calls except the request it forwards, and `keepmum setup`'s one model download. There is no telemetry and no update check.

## Mac app

A menu-bar app that sets keepmum up and runs it in the background, with a dashboard: live flow diagrams of requests going out and replies coming back, per coding agent and provider, with hover highlighting; what was found and masked per type, with switches for what gets masked and an editor for your own rules; scan latency; every request in a history; a searchable Logs page of what was masked; and an Audit page that checks the audit log and lists every change that turned protection down, in light and dark mode. Its local stats feed carries counts, types and rules, and for the Logs page only the first and last character of each value of 8 or more characters (`g…w`) and the text around each value with every masked value in it shown as its type, so it cannot show a whole secret even by mistake. See [mac/README.md](mac/README.md) to build it.

## Accuracy and speed

Measured on synthetic, labeled datasets on an M5 Pro (details in [bench/results/PHASE0.md](bench/results/PHASE0.md), [MODELS.md](bench/results/MODELS.md), [PFSERVE.md](bench/results/PFSERVE.md) and [RULES.md](bench/results/RULES.md)):

| | Result |
| --- | --- |
| Secret recall | 0.989 to 1.000 across five datasets, four of them held out |
| Personal data (`pii = true`) | recall 0.996 to 1.000, precision 0.85 to 0.93 |
| Rules alone (`keepmum model off`) | secret recall 0.993 at precision 1.000; personal data recall 0.77 at precision 0.996 (no names or addresses); 8 to 9 ms for 4K tokens |
| Fake secrets wrongly flagged | 1 of 40 and 0 of 15 on the first two sets; **9, 3 and 3 of 15 on the three freshest held-out sets** (unusual ID formats; see below) |
| Time added per request | about 100 to 145 ms at the 95th percentile for 4K tokens of new text; earlier turns are cached |

No language model judges spans at request time. The Apple on-device model and Qwen3-4B were both measured and dropped: deterministic refinement removed more false positives, faster.

## Limitations

- **False positives on unusual identifiers.** On two fresh sets of hard negatives, 9 of 15 and 3 of 15 were flagged (memory addresses, Azure resource IDs, Stripe object IDs, Kubernetes pod hashes, AWS VPC and security group IDs, Xcode build folders, and IDs right after the word "key", such as idempotency keys). Over-masking costs the model some context but never leaks. A small trained classifier is planned for this.
- **The rules alone miss what only context reveals.** With `keepmum model off`, names, addresses and a secret with neither a known shape nor a telling name around it go out unmasked.
- **Images and PDFs pass through unscanned.**
- **Local transcripts keep real values.** Your agent's history on your disk is restored, so it holds real values. keepmum protects what leaves your Mac, not what is stored on it.
- **Shell commands receive real values.** An agent can still run a command that sends a restored secret somewhere. Use it together with a sandbox such as [Claude Code's](https://code.claude.com/docs/en/sandboxing), whose network allowlist stops that. keepmum controls what the provider sees; the sandbox controls what commands can do.
- **English-first.** The local model is strongest on English text.
- **macOS on Apple silicon only.**
- **Remote Control.** Claude Code turns Remote Control off while its requests go through a proxy, and Remote Control would send the restored transcript to Anthropic around keepmum anyway. Use an unprotected session when you need it.
- **Codex and pi: the Responses and Messages APIs only.** Codex's WebSocket transport is refused (it falls back to HTTPS), and pi providers other than Anthropic and OpenAI (Google, OpenRouter, local models) are not routed through keepmum. Chat Completions requests are blocked, not sent unscanned.
- **opencode: the `anthropic` and `openai` providers only.** opencode Zen, GitHub Copilot, OpenRouter and other providers go straight to their own servers. opencode 2.x works with an API key or a ChatGPT login; opencode 1.x sends a ChatGPT login to chatgpt.com itself, around keepmum, and `keepmum protect status` warns about it.

## Development

```sh
uv sync
uv run pytest                                # detection, proxy (Messages and Responses APIs), streams, settings, clients
uv run python bench/gen.py                   # generate the benchmark datasets
uv run python bench/run.py                   # accuracy and speed; see bench/README.md
swift docs/video/flow.swift flow.mp4         # the 48-second flow video (detect, mask, send, restore), 1080p
```

The design and milestones are in [SPEC.md](docs/SPEC.md).

## Trademarks

Claude, Claude Code and Anthropic are trademarks of Anthropic, PBC. The Mac app shows their logos, from Anthropic's press kit, to identify the services keepmum works with (see [mac/Brand/NOTICE.md](mac/Brand/NOTICE.md)). keepmum is an independent project, not affiliated with or endorsed by Anthropic. OpenAI Privacy Filter is released by OpenAI under Apache-2.0.

## License

Apache-2.0.
