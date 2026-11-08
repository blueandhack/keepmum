# End-to-end cases with real agents

The unit tests check keepmum's functions. These cases check what the user ends up with: a real
Codex, Claude Code or the Antigravity CLI (`agy`), running real tools on a real folder, through keepmum. The questions they answer:

- Did any protected value leave the Mac, whole or in part?
- Does every file the agent creates or renames carry the real name, never `KEEPMUM_…`?
- Does every file hold the real value, byte for byte?
- Did keepmum mask something that was never sensitive, such as a file name?

```sh
.venv/bin/python -m e2e list                          # the cases
.venv/bin/python -m e2e run                           # every case, scripted, on each installed agent
.venv/bin/python -m e2e run fn- --agent codex         # cases by id prefix, one agent
.venv/bin/python -m e2e run --mode live --repeat 3    # the real model, three runs per case
.venv/bin/python -m e2e run base-01 --keep            # keep transcripts and requests in e2e/results/
```

The exit code is 0 when nothing regressed. Results go to `e2e/results/<mode>-<time>/summary.md`
(not committed).

### In CI

The `agents` job in `.github/workflows/ci.yml` runs the scripted suite on every change, with Codex and
Claude Code pinned to the versions this suite was last checked against (`CODEX_VERSION`,
`CLAUDE_CODE_VERSION`). It calls `e2e/ci.sh`, which you can run yourself:

```sh
e2e/ci.sh                  # every case; e2e/ci.sh fn- for some
```

The agents sign in with dummy API keys, in a temporary folder. Nothing reaches a provider in scripted
mode, so the key is never checked, and no real secret is needed. Your own logins (`~/.codex`, the
Keychain) are not read or written. Two consequences:

- CI covers keepmum's API-key routes (`/openai`, the Messages API with `x-api-key`). The ChatGPT-login
  route (`/chatgpt`, zstd bodies) is covered when you run `python -m e2e run` on a Mac signed in that way.
- Cases that need the Privacy Filter model (`config` with `model = true`) skip themselves on a runner
  without it.
- The Antigravity CLI runs in CI with no login at all, through its gateway mode with a dummy key
  (below). It is pinned by its release's URL and SHA-512 (`AGY_URL`, `AGY_SHA512`): its installer only
  installs the newest. Its Google sign-in route (`/antigravity`) is covered live.

To move to a new agent version, run the suite locally with it, then raise the pin in the same change.

## How a run works

```
real agent ──▶ keepmum ──▶ tap ──▶ scripted model    (scripted)
 (codex,      own HOME,    records       or
  claude,     salt and     every byte  the provider   (live)
  agy)
              config       that leaves
```

Each run gets:

- **A fresh keepmum** with its own HOME: a new salt, the case's `config.toml`, its own audit log. It is
  built the way `keepmum serve` builds it (`launch.build_proxy`), so masking, restore, tool policy and
  the checks are the product's.
- **A tap** between keepmum and the provider. It records every request that leaves, decompressed
  (keepmum sends zstd to chatgpt.com), and every reply. The leak check reads it.
- **The real agent binary** with the user's login but not their setup: `codex exec --ignore-user-config
  --ephemeral -s workspace-write`, and `claude -p --setting-sources project,local --strict-mcp-config`.
  User config, MCP servers, hooks and plugins stay out; a case that needs an MCP server gets the stub one. Codex still lists the user's skills, so
  requests differ a little between Macs. The checks look only at the case's own values and files.
- **The Antigravity CLI** (`agy -p --output-format stream-json --mode accept-edits`), pointed at
  keepmum as `keepmum agy` points it, with a session-only `--settings` rule allowing its commands
  (headless, it denies every command no rule allows). Live, it is signed in with Google:
  `CLOUD_CODE_URL` sends its Code Assist calls to keepmum's `/antigravity`; it runs with `--sandbox`,
  reads the user's `~/.gemini/config` (there is no flag to skip it) and keeps each run in its
  conversation history. Its sandbox lets the model retry a blocked command outside it, so a live run
  can reach the network; the cases' values are synthetic. Scripted, it has no login and a HOME of its
  own: its gateway mode (`AGY_LLM_GATEWAY_URL`, a dummy key) sends its model calls to keepmum's
  `/gemini`, and it runs without `--sandbox`, which in a fresh HOME refuses every write to the
  workspace (the commands are the case's own). MCP cases run scripted only: agy takes MCP servers
  only from `~/.gemini/config/mcp_config.json`, which the harness writes only in a run's own HOME;
  `mcp(*)` is allowed for the session, as headless agy denies MCP calls otherwise.
- **A workspace** built from the case's `[files]`, snapshotted before and after.

### Two modes

| | scripted | live |
|---|---|---|
| Model | `scripted.py` plays the case's turns | the provider's real model |
| Deterministic | yes: the same result every run | no: use `--repeat` and read the pass rate |
| Cost | free, about 2 s per case | the user's subscription, 15 to 60 s per case |
| When | every change to keepmum | before a release, and after an agent update |
| Finds | keepmum's own bugs, every time | what real models do with placeholders |

The scripted model writes real tool calls in each agent's own wire format: Claude Code's `Bash`,
`Write`, `Read` and `Edit`, and Codex 0.147's code mode (`exec` running `tools.exec_command` and
`tools.apply_patch`, with plain `exec_command` and `apply_patch` when a Codex offers those; MCP
tools from the script, `tools.mcp__notes__send_note(…)`, in a raw `codex` call), and the
Antigravity CLI's `run_command`, `write_file`, `read_file` and `edit_file` over the Gemini API. The
agent runs them for real. The scripted model copies the placeholder it was actually shown, taken from
the live vault, as a real model would. It splits each placeholder across two stream deltas.

## What every run must satisfy

Whatever a case asks, `oracle.py` checks these, worst first:

| Class | Fails the run when |
|---|---|
| `leak` | any form of a protected value (raw, JSON-escaped once or twice, base64, URL-encoded) is in a request, or a value was masked only in part and the rest was sent; or it went into an MCP call or a web tool that sends from the Mac (WebFetch, a browser) |
| `residue` | a placeholder, in any spelling (`keepmum_secret_…`, `keepmum-secret-…`), is in a file name or file on disk, a command the agent ran, a tool's input, or the final answer |
| `false-mask` | a workspace path or name, or a string the case lists in `plain`, was sent as a placeholder; or a protected value was masked together with the text around it |
| `task` | the workspace is not what the case expects: a missing file, a wrong name, wrong content, an unexpected new file |
| `health` | the agent failed or timed out, keepmum met a placeholder it could not restore, keepmum answered with an error, or the script stopped early |
| `shown` | (reported, not failing) a placeholder in something the user sees but that changes nothing, such as a reasoning summary |

## Writing a case

One TOML file in `cases/`. Put the top-level keys (`prompt`, `plain`, `known_gap`…) **above** the first
`[table]`: TOML assigns a key written below `[files]` to `[files]`, and the loader refuses a case
with no prompt for that reason.

```toml
id = "fn-01-token-in-file-name"
title = "A file whose name holds a token is renamed, and its old name written down"
why = "What breaks for the user if this fails."
prompt = "The task, for live mode. Say 'Do not ask questions.'"
config = "[detect]\nmodel = false\n"   # keepmum's config.toml (default: rules only)
agents = ["codex", "claude", "agy"]    # default: all three
modes = ["scripted", "live"]           # default: both
plain = ["guard-20261104-231405.md"]   # must reach the provider unmasked
workspace_dir = "ws"                   # the workspace folder's name (templates allowed)
known_gap = "…"                        # or { codex = "…" }: see below
mcp = true                             # give the agent the stub MCP server `notes` (below)
mcp_reply = "…"                        # what its `lookup` tool returns (templates allowed)

[values]                               # what keepmum must protect
tok = { gen = "github-token", kind = "SECRET" }        # fresh every run: github-token, anthropic-key, stripe-key
person = { text = "Margaret Thornbury", kind = "PERSON", must_mask = false }

[files]                                # the workspace; paths and contents take templates
"backup-{{v:tok}}.txt" = "nightly backup\n"

[[script]]                             # scripted mode: one table per model turn
calls = [{ shell = "ls" }]
[[script]]
calls = [{ shell = "mv backup-{{ph:tok}}.txt backup.txt" },
         { write = "MOVED.md", content = "backup-{{ph:tok}}.txt\n" }]
[[script]]
say = "Done."

[expect]
masked = ["tok"]                                   # reached the provider as its placeholder
files = { "backup.txt" = "nightly backup\n",       # exact content, or a table:
          "MOVED.md" = { regex = "^backup-{{v:tok}}\\.txt\\n?$" } }   # contains, lacks, regex, equals
absent = ["backup-{{v:tok}}.txt"]
only_new = ["backup.txt", "MOVED.md"]              # globs; anything else new fails
others_unchanged = true
answer_contains = []
residue_ok = []                                    # files where a kept placeholder is right
commands_may_keep = false                          # commands may hold placeholders (a kept token, a refusal)
unresolved = 0                                     # placeholders keepmum is expected not to restore
agent_may_fail = false
mcp_calls_min = 1                                  # calls the MCP server must receive
mcp_contains = []                                  # text it must receive (templates)
mcp_may_receive = []                               # values it may get real (a type its setting allows)
web_calls_min = 1                                  # web searches or fetches the agent must make
warned = ["script"]                               # checks that must warn (let the request through, flagged)
warn_shown = true                                  # the agent must show the user a `keepmum: …` note
```

A script call can carry one form per agent, for a turn they make differently: `{ shell = "…", codex =
"<script>" }` is the script for Codex and the command for Claude Code and agy, and a call with only
`codex`, only `claude` or only `agy` is made by that agent only.

### The MCP server

`mcp_notes.py` is a stdio MCP server, standard library only, with two tools: `send_note(text)` and
`lookup(name)`. It stands in for a connector (Slack, Gmail): what it receives would leave the Mac. With
`mcp = true`, Codex gets it through `-c mcp_servers.notes…` (its tools approved, as `codex exec` would
cancel them otherwise) and Claude Code through `--mcp-config`. It writes each call's arguments to a
log the oracle reads: a protected value in the log, or in the MCP call the agent's transcript shows
(a call that failed shows its input too), is a `leak`, unless the case lists it in `mcp_may_receive`.
Placeholders in an MCP call are not `residue`: keeping them there is the point.

### Web tools

A web search or fetch sends its input off the Mac. Claude Code's `WebSearch` and `WebFetch` and
Antigravity's `read_url_content` and `browser_*` tools are read from the transcript like MCP calls:
a protected value in their input is a `leak`. Codex searches at OpenAI (with a ChatGPT login,
through its `codex_apps` connector, an MCP call). Antigravity's `search_web` is a Code Assist call
(`requestType: "web_search"`) that goes back through keepmum, so the leak check on requests covers
it. `web_calls_min` makes sure the agent searched at all.

Templates: `{{v:x}}` is the real value (files, expectations). `{{ph:x}}` is the placeholder the model
was shown for it (scripts). `{{ph:x|lower}}`, `|slug`, `|upper` and `|typo` give a placeholder as a
careless model rewrites it. `{{seen:text}}` is text as the model saw it, with every masked value
replaced; use it for a name the model copies from a listing. `{{cwd}}` is the workspace path as the
agent told the model. Script calls: `shell`, `write` + `content`, `read`, `edit` + `old` + `new`,
and raw `codex = "<JavaScript>"`, `claude = {name, input}` or `agy = {name, args}`.

Results never hold a case's values: everything written shows them as `{{v:name}}`. `--keep` also
writes the requests, which hold whatever else the agent sent (paths, skill lists). Do not share them.

## Known gaps

A case that shows a bug not fixed yet carries `known_gap`, for all agents or per agent. It runs
like any other and is reported as `◌` with its findings, but does not fail the run. When a fix makes
it pass, it is reported as `!` and **fails the run**, until the marker is removed. From then on the
case guards against a regression.

A bug report becomes a case in this order: write the case so it fails in scripted mode, mark it
`known_gap`, fix keepmum, remove the marker.

## Adding an agent

Subclass `agents.Agent`: `command()` points the binary at keepmum's port, and `transcript()` reads its
output into what it said, ran and showed. If it speaks a new wire format, teach `scripted.py` to
answer in it.
