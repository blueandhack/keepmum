---
title: Leave a project out
description: Let Claude Code in one project go straight to the API, unscanned.
---

```sh
keepmum exclude ~/Projects/some-repo     # Claude Code there goes straight to the API, unscanned
keepmum exclude --list
keepmum include ~/Projects/some-repo     # scan it again
```

`keepmum exclude` sets `env.ANTHROPIC_BASE_URL` to keepmum's upstream in the project's `.claude/settings.local.json` (at the git repository's root, as Claude Code reads it), which outranks `keepmum protect`. That file is never committed: keepmum refuses if git tracks it and otherwise adds it to `.git/info/exclude`.

Claude Code applies it once you trust the folder; until then the project still goes through keepmum. Open sessions there switch at once when you exclude, but keep going straight to the API after `keepmum include` until they restart. In an excluded project the VS Code wrapper starts Claude Code without keepmum, and `keepmum claude` refuses. The Mac app lists exclusions in Settings.
