---
title: Accuracy and speed
description: Recall, false positives and time added per request, measured on labeled datasets.
---

Measured on synthetic, labeled datasets on an M5 Pro (details in [bench/results/PHASE0.md](https://github.com/blueandhack/keepmum/blob/main/bench/results/PHASE0.md), [MODELS.md](https://github.com/blueandhack/keepmum/blob/main/bench/results/MODELS.md), [PFSERVE.md](https://github.com/blueandhack/keepmum/blob/main/bench/results/PFSERVE.md) and [RULES.md](https://github.com/blueandhack/keepmum/blob/main/bench/results/RULES.md)):

| | Result |
| --- | --- |
| Secret recall | 0.989 to 1.000 across five datasets, four of them held out |
| Personal data (`pii = true`) | recall 0.996 to 1.000, precision 0.85 to 0.93 |
| Rules alone (`keepmum model off`) | secret recall 0.993 at precision 1.000; personal data recall 0.77 at precision 0.996 (no names or addresses); 8 to 9 ms for 4K tokens |
| Fake secrets wrongly flagged | 1 of 40 and 0 of 15 on the first two sets; **9, 3 and 3 of 15 on the three freshest held-out sets** (unusual ID formats; see [Limitations](/docs/about/limitations/)) |
| Time added per request | about 100 to 145 ms at the 95th percentile for 4K tokens of new text; earlier turns are cached |

No language model judges spans at request time. The Apple on-device model and Qwen3-4B were both measured and dropped: deterministic refinement removed more false positives, faster.

For the optional checks' numbers, see [Sensitivity](/docs/checks/sensitivity/).
