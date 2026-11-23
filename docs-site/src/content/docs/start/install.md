---
title: Install
description: Requirements, the Mac app with everything included, and the command line.
---

## Requirements

- A Mac with Apple silicon, and macOS 14 or later
- Claude Code, Codex, the Antigravity CLI (`agy`), Gemini CLI, pi or opencode
- For the command line only (the Mac app bundles all of it): Python 3.12+, [uv](https://docs.astral.sh/uv/), and Xcode or its command line tools (to build pf-serve)
- 1.6 GB of disk for the local model. While agents are working it takes about 0.2 GB of memory of its own, and macOS keeps its 1.6 GB file in its cache, which it lets go when memory is short; after 10 minutes without a request it unloads, and loads again in about a quarter of a second. Neither with `keepmum model off`. The [optional checks](/docs/checks/) add their own while in use: 0.4 GB (injection) and 0.4, 1.1 or 4.9 GB (harm)

## The Mac app (everything included)

Download `Keepmum-<version>.dmg` from the [Releases](https://github.com/blueandhack/keepmum/releases) page (or build it: [mac/README.md](https://github.com/blueandhack/keepmum/blob/main/mac/README.md)), drag Keepmum into Applications, open it, and follow **Setup**: download the model, start the background service, and switch on protection for each coding agent you use (Claude Code, Codex, pi, opencode; the Antigravity CLI and Gemini CLI run through `keepmum agy` and `keepmum gemini`, see [Use with your agents](/docs/start/agents/)). The app bundles keepmum, its Python runtime and pf-serve; nothing else needs installing. Every session of a protected agent then goes through keepmum; for Claude Code that is the terminal, VS Code and the desktop app.

## The command line

```sh
git clone https://github.com/blueandhack/keepmum.git && cd keepmum
uv tool install --editable .                  # installs `keepmum` and `keepmum-claude-wrapper`
native/pf-serve/build.sh                      # builds pf-serve from pinned sources into ~/.local/share/keepmum/bin
keepmum setup                                 # downloads the model (1.6 GB, once), checked against its pinned SHA-256
keepmum doctor                                # every line should be ✓ (the app: Settings › Check this Mac)
keepmum selftest                              # sends a canary secret through keepmum to a local mock; fails if it leaks
```

Next: [use keepmum with your agents](/docs/start/agents/), or [run it in the background for every session](/docs/start/background/).
