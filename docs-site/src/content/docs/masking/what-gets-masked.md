---
title: Choose what gets masked
description: Personal data, types that pass through, your own patterns, exceptions and built-in rules.
---

```sh
keepmum pii on                                                       # also mask personal data: names, emails, phones, addresses (off by default)
keepmum types                                                        # which types are masked
keepmum types pass person date                                       # let names and dates through unmasked; `keepmum types mask person` undoes it
keepmum rules mask 'EMP-\d{6}' --name "Employee ID" --type account   # always mask your own pattern
keepmum rules allow acme-test-key                                    # never mask this exact text (or --pattern for a regex)
keepmum rules off key-in-prose --yes                                 # turn off a built-in rule that masks what it should not (`keepmum rules on` undoes it)
keepmum rules builtin                                                # every built-in rule, its type, and whether it is on
keepmum rules list                                                   # and `keepmum rules remove mask 0`
```

Personal data is off by default: only secrets and your own rules are masked until you turn it on (`keepmum pii on`, or Masking › Personal data in the Mac app). Letting secrets through, or turning off a rule that finds secrets, needs `--yes`.

A rule turned off stops masking only what it alone finds: other rules and the local model still apply, so for one value an exception is narrower. Values masked earlier in a session follow your changes: once every detector that found a value is off, or the value has an exception, it goes as is. Your own mask rules apply even with personal data off.

Running proxies pick up every change on their next request; the Mac app's Detection page edits the same settings and shows, per type, how many values were found, allowed by an exception, passed through by your settings, and masked. The same settings live in the [config file](/docs/reference/configuration/) under `[detect]`, `[rules]` and `[allowlist]`.
