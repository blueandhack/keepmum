---
title: Development
description: Run the tests, the benchmarks and these docs.
---

```sh
uv sync
uv run pytest                                # detection, proxy (Messages and Responses APIs), streams, settings, clients
uv run coverage run -m pytest && uv run coverage report   # statement and branch coverage; CI fails below 95%
mac/lint.sh                                  # SwiftLint on the Mac app (pinned, checked; mac/README.md)
uv run python bench/gen.py                   # generate the benchmark datasets
uv run python bench/run.py                   # accuracy and speed; see bench/README.md
swift docs/video/flow.swift flow.mp4         # retired: the MP4 of the flow; keepmum.app draws it with site/flow.js, its source now
```

The design and milestones are in [SPEC.md](https://github.com/blueandhack/keepmum/blob/main/docs/SPEC.md).

## These docs

The docs are Markdown in [`docs-site/src/content/docs/`](https://github.com/blueandhack/keepmum/tree/main/docs-site/src/content/docs), built with [Starlight](https://starlight.astro.build) into `site/docs/` and published with the rest of keepmum.app. A change that changes what keepmum does should change its page in the same commit.

```sh
cd docs-site
npm ci
npm run dev        # http://localhost:4321/docs/, reloads as you edit
npm run build      # into site/docs/, as the Site workflow does
```

## Trademarks

Claude, Claude Code and Anthropic are trademarks of Anthropic, PBC. The Mac app shows their logos, from Anthropic's press kit, to identify the services keepmum works with (see [mac/Brand/NOTICE.md](https://github.com/blueandhack/keepmum/blob/main/mac/Brand/NOTICE.md)). keepmum is an independent project, not affiliated with or endorsed by Anthropic. OpenAI Privacy Filter is released by OpenAI under Apache-2.0.

## License

Apache-2.0.
