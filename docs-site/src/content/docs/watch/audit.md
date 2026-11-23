---
title: Audit what keepmum did
description: A tamper-evident log of every request, every change to keepmum's protection, and each start and stop.
sidebar:
  label: Audit log
---

keepmum keeps a tamper-evident audit log: every request (who sent it, where it went, counts by type, which rules found what, and which tools got real values back or kept placeholders), every change to its protection (marked when it turns protection down, with the command that made it, or "config file" for an edit by hand), agents and projects taken out of its path, and each start and stop, so gaps in coverage show. Never a value, a preview or a placeholder: allowlist entries and your own patterns appear as keyed fingerprints.

```sh
keepmum audit list --weakening --since 30d   # what turned protection down, and who did it
keepmum audit verify                         # exits 1 if an entry was edited, added, removed or reordered
keepmum audit head                           # the newest <seq>:<hash>: keep it somewhere else
keepmum audit verify --anchor 4211:9be41f07c2d8a35e   # the log still holds that entry, unchanged
keepmum audit export --since 30d -o audit.jsonl       # for a review or a ticket (or --format csv)
keepmum audit verify --file audit.jsonl               # check an export
keepmum audit keep 365                                # keep entries for a year (90 days by default)
```

Each entry carries the hash of the one before, so a change inside the log breaks the chain where it happened. Someone who can write the file could still rewrite it from some point on, or cut off its end; a head kept elsewhere catches that.

The Mac app's Audit page checks the chain each time it opens, against the head it saved at its last good check, and sets the retention and exports the log too. Entries are kept for `[audit] keep_days` (90 days), and a shorter retention is itself recorded as weakening.
