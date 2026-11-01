# keepmum — Local Secret & PII Redaction for Coding Agents (Build Spec)

Oct 25, 2026 · @Yujia Lin · updated Oct 29, 2026 for Codex, pi and opencode, pf-serve, the Mac app, and the rules without the model

## Summary

keepmum is a local proxy that removes secrets and personal data from coding-agent requests (Claude Code, Codex, pi, opencode) before they leave the Mac, then restores the real values locally. Nothing is sent to any third party: detection runs fully on-device. The name comes from "keep mum": to say nothing about a secret.

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

- ~~Other AI clients (Codex, Cursor, Gemini CLI).~~ Codex and pi were added on Oct 27, 2026, opencode on Oct 29 (Milestones). Cursor and Gemini CLI remain out of scope.
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
| Distribution | Keepmum.app (self-contained, notarized DMG) and `uv tool install`; a Homebrew cask later |
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
| Assistant text | Restore |
| `thinking` blocks | Keep placeholder (signed; must be resent unchanged) |
| `tool_use` input for Edit, Write, MultiEdit, NotebookEdit | Restore |
| `tool_use` input for Bash | Restore (open question below) |
| `tool_use` input for WebFetch, WebSearch, and every MCP tool (`mcp__*`) | Keep placeholder. keepmum cannot tell which MCP servers reach the network, so all of them keep placeholders unless the config says otherwise |

The policy lives in config (`[restore]` per tool name), so it can be tightened without code changes.

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
| `keepmum serve` | Runs the proxy standalone on `127.0.0.1:7788` (configurable). Binds to localhost only. |
| `keepmum protect on\|off\|status [--client]` | Points Claude Code, Codex, pi and opencode at `keepmum serve` in their own config files, or undoes it. |
| `keepmum observe on\|off` | Observe only: scan and count, but forward requests unchanged. |
| `keepmum model on\|off` | The rules and the local model, or the rules alone. |
| `keepmum types` / `keepmum rules` | What gets masked: types passed through, your own mask rules, exceptions, built-in rules turned off. |
| `keepmum exclude` / `keepmum include` | Lets one project's Claude Code skip keepmum. |
| `keepmum status` | Setup, protection and settings as JSON, for the Mac app. |
| `keepmum audit list\|verify\|head\|export` | The tamper-evident audit log: requests, protection changes, starts and stops. |
| `keepmum scan <file or ->` | Prints findings with values masked; exits 1 if anything is found. Usable as a pre-commit hook. |
| `keepmum setup` | Downloads the Privacy Filter model once and checks its pinned SHA-256. |
| `keepmum doctor` | Checks macOS version, Apple silicon, pf-serve and the model (with a canary), the install salt, protection, routes around the proxy, port free. |
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

[restore]                            # per tool: "restore" or "keep"; prefixes like "mcp__files__*" allowed
default = "restore"
WebFetch = "keep"
WebSearch = "keep"
"mcp__*" = "keep"

[log]
level = "info"                       # never logs values, only type + counts

[audit]
keep_days = 90                       # audit log retention; shortening it is recorded as weakening
```

`keepmum claude` writes logs to `~/.config/keepmum/keepmum.log` (mode 0600), because the terminal belongs to Claude Code, and records its port in `~/.config/keepmum/runtime.json` (0600) for the Mac app. It refuses to start if any Claude Code settings file sets `ANTHROPIC_BASE_URL` or a Bedrock/Vertex/Foundry switch, or if such a switch is in the environment, since that could route traffic around the proxy. An `ANTHROPIC_BASE_URL` already in the environment (another gateway) becomes keepmum's upstream.

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
- [x] **opencode (Oct 29, 2026).** `keepmum protect` sets `provider.anthropic` and `provider.openai` `options.baseURL` in `~/.config/opencode/opencode.json` (the 1.x shape, which 2.x migrates). opencode keeps one OpenAI base URL for both logins, so an `/openai/v1` request that carries `chatgpt-account-id` is routed to chatgpt.com; scanned bodies sent there are zstd-compressed, since that backend refuses large uncompressed ones. An upgrade request that arrives as plain HTTP (uvicorn without a WebSocket library) is refused, not forwarded. Verified with opencode 2.2.1 (`opencode run`) against a local stand-in for both providers: 8 requests, including session titles, carried placeholders only, the replies showed the real value, and the WebSocket attempt fell back to HTTPS. A plugin was ruled out: plugin load errors are swallowed and `--pure` skips plugins, so protection would vanish silently, and no hook sees model output before it becomes a tool call. opencode 1.x with a ChatGPT login is not covered and is reported.
- [x] **More rules, and the rules without the model (Oct 29, 2026).** 69 more L1 rules (96): 52 provider formats from gitleaks and TruffleHog, 11 for secrets in flags, config files and sentences, and 6 for personal data (emails, phones, SSNs, dates of birth). `detect.model = false` runs L1 alone and switches while running. Measured on dev and a new held-out set written before the rules (`bench/results/RULES.md`): with the rules alone, secret recall went from 0.78 to 0.99 and personal-data recall from 0.39 to 0.77, at precision 0.996, with no hard negative flagged; the earlier held-out sets flag exactly what they flagged before.
- [x] **Built-in rules turned off (Oct 29, 2026).** `[rules] off` (`keepmum rules off|on`, the Detection page's Top rules) turns off a rule or check by name; rules that find secrets need `--yes` or a confirmation. The vault records which detectors found each value, so the known-values pass lets a value masked earlier go once every detector that found it is off or was your own rule and is gone, or the value has an exception. Before this, an exception added mid-session did not unmask a value already masked.
- [x] **Logs page (Oct 29, 2026).** `GET /_keepmum/findings` (stats token) lists the values masked in new text, newest first: type, detector and rule, length, agent, outcome, and the first and last character of values of 8 or more characters. Kept in memory (the last 2,000), never in `history.jsonl`; `[log] previews = false` drops the characters, including those already kept. The Mac app's Logs page searches and filters it. This relaxes the earlier rule that the feed carries no part of a value, at the user's request; values under 8 characters still show none.
- [x] **Logs context (Nov 1, 2026).** Each finding also carries up to 80 characters of the text before and after the value, with every masked value and placeholder in that window, whole or cut by it, shown as its type (`‹SECRET›`): in mask mode, what the provider received. Memory only, with the findings; `[log] context = false` drops it, including what was kept, and is recorded in the audit log. The Mac app shows it in a row's details, and draws each value as its first and last character around one star per hidden character.
- [x] **Audit log (Oct 30, 2026).** The first of three privacy and security features that set keepmum apart from general gateways (then an exposure scan of local transcripts, then an egress guard for shell commands). See *Audit log* below. Verified end to end with `keepmum serve` against a local stand-in provider: start, the request with its restore decision, an `observe on` marked as weakening, and the stop on SIGTERM, in a chain that verifies, with no value or placeholder in the file.
- [ ] **M6 Ship.** README, Homebrew tap, name check on GitHub, PyPI and Homebrew. *Oct 26, 2026: Keepmum.app is self-contained (bundled Python runtime, pf-serve, a LaunchAgent via SMAppService) and sets itself up: model download, background service, `keepmum protect`. Left: Developer ID signing and notarization, a DMG, a Homebrew cask, updates, and Windows (pf-serve already builds there; the app would need its own UI).*

## Audit log

**Why.** A masking proxy says what it masks; it cannot show afterwards what it did, or that nobody turned it down. The audit log answers "what left this Mac, what got real values back, and who weakened protection, when", in a form that shows tampering. General gateways keep a masking record but not one that can be checked, and they do not record changes to their own settings.

**File.** `~/.config/keepmum/audit.jsonl` (0600), one canonical JSON entry per line (sorted keys, no spaces), shared by every keepmum process: the app's service, `keepmum claude` and `codex`, the VS Code wrapper, and the CLI. Appends take an exclusive `flock` on `audit.lock`, read the last whole entry, and write one line, so concurrent processes keep one chain. `audit.head.json` holds the newest `seq` and `hash`, and the last recorded settings state.

**Entry.** `seq` (one more than the entry before), `ts`, `event`, the event's fields, `prev` (the hash of the entry before; 64 zeros for the first) and `hash` = SHA-256 of the entry's canonical JSON without `hash`.

| Event | Fields | Weakens |
| --- | --- | --- |
| `request` | client, provider, path, outcome, status, `masked` by type, `new`, `found` (detector or rule id → count, new text only), `restored` by type, `tools` ([{tool, action restore\|keep, values}]), `unresolved` | |
| `config` | `by` (the keepmum command, "(from Keepmum.app)" when the app ran it, or "config file" for an edit seen by a running proxy), `changes` ([{setting, from/to or added/removed, weakens}]), `weakens`; the first one is a `baseline` with the full state, compared with the defaults | when any change weakens |
| `protect` | action, clients, by | off |
| `exclude` / `include` | project, by | exclude |
| `start` / `stop` | mode, pid, and at start the port, version, detector, pii, observe | |
| `pruned` | dropped, `through` {seq, hash}, keep_days | |

Bodyless requests that carry no content (a GET of `/v1/models`) are not audited; blocked and rejected ones always are. A tool call is listed when its input held placeholders: `restore` means the tool ran with real values, `keep` that it kept the placeholders (WebFetch, WebSearch, MCP tools by default).

**Never recorded:** values, previews, placeholders, headers, bodies, command arguments. Allowlist strings and patterns, and the patterns of your own mask rules, are recorded as fingerprints: the first 12 hex digits of HMAC-SHA256(install salt, kind + value). They show that an entry was added or removed, and which, without what it says.

**What weakens.** observe turned on; the model or personal data turned off; a type passed through; a built-in rule turned off; one of your mask rules removed; an allowlist entry added; a tool switched from keep to restore; a new upstream (flagged for a look); a shorter retention; `protect off`; `exclude`.

**Who notices a change.** The CLI records it right after writing the file. A running proxy records changes it sees when the file changes (an edit by hand). At start, a proxy records changes made while none was running. The last recorded state in `audit.head.json` makes each change recorded once, whichever process sees it first.

**Verify.** `keepmum audit verify` checks every entry's hash, every `prev` link and every `seq` step, that the log starts at the beginning or where a `pruned` entry says it was cut, and that it is not shorter than `audit.head.json` says. `--anchor <seq>:<hash>` checks the log still holds that entry with that hash (at least 12 hex digits; an anchor older than a recorded pruning cannot be checked and is not an error). `--file` checks an `audit export`, which starts and ends wherever its period did.

**Limits, stated where users read them.** Someone who can write the file can rewrite it from some point on, recomputing every hash, and update `audit.head.json` too; only a head kept elsewhere catches that. `keepmum audit head` prints it; the Mac app keeps the head of its last good check and verifies against it. A keyed signature would not help: its key would be in a file the same user can read, because the Keychain cannot be shared by keepmum's processes without a prompt (the reason the install salt moved to a file). An audit write that fails is logged and the request goes on: auditing never blocks a request, and the gap shows as a start with no stop or a missing `seq`.

**Retention.** `[audit] keep_days` (90; 1–3650). The background service prunes at start: entries older than that are removed, and a `pruned` entry records how many and the hash they ended with. Pruning never passes an unreadable line.

**CLI.** `keepmum audit list [--since 7d] [--weakening] [--no-requests] [--limit 50] [--json]`, `verify [--anchor] [--file] [--json]`, `head [--json]`, `export [--since 30d] [--format jsonl|csv] [-o file] [--weakening]`. An export file is written 0600.

**Mac app.** The Audit page runs `keepmum audit list --no-requests` and `verify --anchor <saved head>` when it opens and every 15 s, shows whether the log is intact, the head with a copy button, and the changes, newest first, with the weakening ones marked and who made them. It saves the head only after a good check. The app runs every CLI command with `KEEPMUM_CALLER=Keepmum.app`.

## Testing, safety rules, risks

The one test that must never fail: a canary secret sent through keepmum never appears in any byte that reaches the upstream.

**Testing**

- Unit tests per detector, against the benchmark dataset.
- Round-trip property test: for any text, `restore(redact(text)) == text`.
- Streaming property test: split SSE events at every byte offset; output must equal the non-streamed restore.
- Proxy tests against a local mock upstream that records every byte received and asserts no canary is present.
- Fail-closed tests: kill L2 mid-request, make L2 return garbage, send malformed JSON. Each must return an error to Claude Code, never forward the raw body.

**Safety rules for the code**

- Logs contain only type, placeholder and counts. Never values, request bodies or auth headers.
- Bind to `127.0.0.1` only. Reject requests with a non-local `Host` header.
- Third-party loggers (httpx, httpcore, uvicorn) are capped at WARNING so they cannot print URLs or request details; uvicorn's access log is off.
- **Stats feeds** (for the Mac app): `GET /_keepmum/stats` has request counts by outcome, masked and restored counts by type, the detection report, scan latency, and the last 200 events (time, path, status, client, provider, counts); `/_keepmum/history` pages the history file and `/_keepmum/flows` totals it for the flow diagrams. None holds a value or a placeholder. It requires `Authorization: Bearer <token>`, with the token in `~/.config/keepmum/stats-token` (mode 0600).
- No telemetry, no update checks, no network calls except the forwarded request.
- Synthetic secrets only in the repo. Add `keepmum scan` as a pre-commit hook on the keepmum repo itself.

**Risks**

| Risk | Mitigation |
| --- | --- |
| Missed secret (false negative) | Layered detection; redact-when-in-doubt merge; recall target in Phase 0 |
| Over-redaction breaks Claude's work | Allowlist; hard-negative tests; restore in edits so files stay correct |
| Prompt injection asks Claude to send a placeholder somewhere, and restore leaks it | Per-tool restore policy; network tools keep placeholders |
| Secrets inside images or PDFs | Out of scope for v1; documented in README |
| A client opens a WebSocket keepmum cannot scan | Refused; Codex falls back to HTTPS, and `keepmum protect` turns Codex's WebSockets off |
| A user lets a type through, or excludes a project | Explicit and visible: secrets need confirmation, the dashboard marks passed types and lists exclusions |
| Local transcript files keep real values | Out of scope for v1; documented in README |
| Refinement drops a real value | Each refinement rule is tested and measured on held-out data for recall loss (none so far); refinement never touches L1 rule hits |
| Claude Code changes its request format | Walk payloads generically by block type; selftest in CI |

**Open questions**

- Should Bash tool inputs get restored values? Restoring lets commands like `curl -H "Authorization: ..."` work, but it is also the easiest exfiltration path.
- ~~Should `pii` default to on?~~ No (Phase 0): with PII on, 17–27% of hard negatives are flagged, and ACCOUNT, URL and DATE precision is 0.13–0.36.
- ~~Should the OS floor come down?~~ Yes: macOS 14, now that the Apple model is gone.
- ~~Is a Swift binary worth it?~~ Not now: the app bundles Python (about 95 MB) and pf-serve, which also builds for Windows and Linux.
