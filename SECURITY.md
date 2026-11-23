# Security policy

keepmum exists to keep secrets from leaving your Mac, so a leak is the most serious kind of bug it can have.

## Reporting a vulnerability

Please report privately through GitHub: **Security → Report a vulnerability** on this repository. Do not open a public issue.

Useful reports include:

- A request shape that reaches the upstream with a real value in it (a leak).
- A way to make keepmum forward a request it should have blocked (fail-open).
- A way to make keepmum write a value to disk, a log, the history file, or the stats feeds.
- A way to get a request out unscanned through a route keepmum should block: an unscanned endpoint, an encoding it cannot read, a WebSocket.
- A way for a model reply to send a restored value to a network tool.

Please use synthetic secrets in your report, never real ones. A minimal payload plus the `keepmum` version and `keepmum doctor` output is ideal.

## What is out of scope

These are known limits, documented in [Limitations](https://keepmum.app/docs/about/limitations/):

- Detection misses on secret formats with no rule and no context the local model recognizes. Please still report them as regular issues; they become new rules.
- Images and PDFs, which pass through unscanned.
- Real values in your agent's local transcripts, and in shell commands it runs on your Mac.
- What you chose to let through: observe mode, types you set to pass, exceptions you added, and excluded projects.

## Supported versions

keepmum is pre-release. Only the latest commit on `main` gets fixes.
