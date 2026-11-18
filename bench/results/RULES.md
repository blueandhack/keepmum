# More rules, and the rules without the model

Oct 29, 2026 · Apple M5 Pro, 64 GB, macOS 27.2. Raw tables: before [`rules-before-dev.md`](rules-before-dev.md), [`rules-before-holdout5.md`](rules-before-holdout5.md); after [`rules-dev.md`](rules-dev.md), [`rules-holdout5.md`](rules-holdout5.md).

The change: 69 more rules (96 in all), and `detect.model = false` (`keepmum model off`), which runs the rules without the model.

- **Provider keys (52).** Formats from gitleaks (`config/gitleaks.toml` at `5f6ec56`, Oct 23, 2026) and TruffleHog's detectors (Groq, xAI, OpenRouter, Replicate, Supabase, Docker Hub, LangSmith), each with a prefix that makes it safe to match on its own: AI providers, package registries, cloud and CI tokens, Vault, 1Password, age.
- **Secrets in context (11).** Command-line flags named for a secret (`--token X`, `--password=X`), `curl -u user:password`, a password or key in a sentence ("the staging API key is X"), `.netrc`, Docker's `config.json`, `.npmrc` `_auth`, kubeconfig `client-key-data`, Azure storage keys, presigned URL signatures, session cookies.
- **Personal data (6), with `pii = true` only.** Emails, North American and international phone numbers, phone numbers after a label, US Social Security numbers after a label, and dates of birth after a label. Their spans pass the same noise checks as the model's: reserved addresses (RFC 2606), role mailboxes (RFC 2142, also applied to the model now), fictional 555-01xx numbers.

Two new data sets:

- `extra.jsonl` (54 records, generated after the other files so they stay as they were): the new provider formats with no telling variable name, secrets in flags, prose and config files, and personal data in shapes the rules target.
- Held-out 5 (seed 12, `HOLDOUT5_NEGATIVES`): 15 hard negatives written before the rules. They are shaped like the new patterns: git remotes, `icon@2x.png`, `x@self.weight`, `user:${PASSWORD}@host`, versions and IPs, numbers that look like phones, invalid SSNs, a DOB validation message, `--password-stdin` and `--token-file`, keys in prose that are identifiers.

`bench/run.py`'s `rules` detector now runs the pipeline with no model, as keepmum does with the model off. It used to run the bare detector, without the personal-data noise checks.

## Secrets (`pii = false`)

| Detector | Dev recall | Dev negatives | Held-out 5 recall | Held-out 5 negatives |
| --- | --- | --- | --- | --- |
| rules, before | 0.783 | 0/40 | 0.790 | 0/15 |
| **rules, after** | **0.993** | **0/40** | **0.993** | **0/15** |
| rules + model, before | 0.902 | 1/40 | 0.930 | 1/15 |
| **rules + model, after** | **0.993** | **1/40** | **1.000** | **1/15** |

Precision with the rules alone stays 1.000. The one secret the rules miss on both sets is a password in a chat reply ("it's X (changes every quarter)"), found only from context.

## Personal data (`pii = true`)

| Detector | Dev recall / precision | Held-out 5 recall / precision | Held-out 5 negatives |
| --- | --- | --- | --- |
| rules, before | 0.391 / 1.000 | 0.407 / 1.000 | 0/15 |
| **rules, after** | **0.770 / 0.996** | **0.779 / 0.996** | **0/15** |
| rules + model, before | 0.965 / 0.894 | 0.967 / 0.858 | 5/15 |
| **rules + model, after** | **0.997 / 0.913** | **1.000 / 0.871** | **5/15** |

The rules alone find every email, phone number, card number, IBAN, SSN and date of birth in these sets. They find no names, addresses or personal URLs: that takes the model.

## Tuned on held-out 5

The first run of held-out 5 flagged three negatives with the rules alone. Fixing them means held-out 5 is now tuned on:

- `scp … deploy@build-01.acme.dev:/srv`: the email rule backtracked to `deploy@build-01.acme`. An address can no longer end before a dot and more of a host name.
- `noreply@github.com` and a fictional 555-01xx number: the benchmark's `rules` ran without the noise checks (above), so these were the benchmark's error, not keepmum's.

Dev's `To: support@acme-shop.com` (a role mailbox) and an address the email rule found inside a password in a connection string (an address must now start after a space, a quote, a bracket, `=`, `,` or `mailto:`) were fixed on dev.

## Nothing new flagged on the earlier held-out sets

On held-out 1 to 4, the same negatives are flagged before and after, with the rules alone and with the model, in both scopes: 0, 2, 0 and 0 of 15 with the rules; 0, 9, 3 and 3 of 15 with the model.

## Speed

The rules take 8 to 9 ms at the 95th percentile for 4K tokens (5.7 ms before) and 15 to 19 ms at 8K (11 ms before). With the model, this session measured about 230 ms at 4K both before and after the change; earlier sessions measured 95 to 110 ms on the same machine, so the difference is the machine's load, not the rules.

## Repeated characters (Nov 17, 2026)

The `repeated` filter dropped any value with a run of six identical characters, as a placeholder (`xxxxxx`, `000000`, `******`). A real token can hold such a run by chance: among random Slack app tokens, one in about 6,700 has six of a kind in its 90 characters, and went out unmasked (found by `test_provider_formats_with_no_telling_name`, which generates new tokens each run). Now runs of six or more mark a placeholder when they are at least a third of the value (`rules.REPEATED_SHARE`).

A targeted set, since the data sets below hold no such value: 1,520 tokens in every provider format of `tests/test_formats.py`, each with a run of 6 to 8 of one of its own characters at a random place, and 36 placeholders written as people write them (`ghp_xxxx…`, `sk-proj-abc123xxxx…`, `AKIAXXXX…`, `xoxb-000000000000-…`, `******`), each in 5 contexts (`export X=…`, `KEY=…`, `api_key = "…"`, a sentence, a Bearer header):

| Filter | Tokens with a run, masked | Placeholders masked |
| --- | --- | --- |
| any run (before) | 0/1,520 (0%) | 0/180 |
| runs ≥ 20% of the value | 1,323/1,520 (87.0%) | 0/180 |
| runs ≥ 25% | 1,433/1,520 (94.3%) | 0/180 |
| **runs ≥ a third (after)** | **1,503/1,520 (98.9%)** | **0/180** |

The 17 still missed are 24-character Alibaba access key IDs with an injected run of 8, a third of the value: by chance, practically never.

On the data sets, nothing changed: `rules`, `rules+pf-serve`, `rules:pii` and `rules+pf-serve:pii` on dev, held-out 4 and held-out 5 have the same recall, precision and negatives flagged before and after, and the same lists of missed and flagged spans.
