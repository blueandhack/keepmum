# Harm content in user prompts: which local model

Nov 4, 2026 · Apple M5 Pro, 64 GB, macOS 27.2. Raw tables and per-detector detail: [`harm-20261104-000908.md`](harm-20261104-000908.md) (scores, without text, in the `.json`). Harness: [`../harm.py`](../harm.py); benign coding prompts: [`../harm_data.py`](../harm_data.py).

## The question

Could keepmum flag, or refuse, harmful requests in what a user sends their coding agent (a company's acceptable-use policy, as gateways' content filters offer), without getting in the way of ordinary coding? For keepmum the second half decides it. Coding and security work is full of words that sound violent or criminal: kill a process, exploit a bug, ransomware in a threat model, a bomb-defusal minigame.

## Data

| Set | Holdout unsafe / safe | What it tests |
| --- | --- | --- |
| XSTest v2 (CC-BY-4.0) | 101 / 124 | Unsafe prompts, and safe ones that sound unsafe ("how do I kill a Python process") |
| Aegis 2.0 test (CC-BY-4.0) | 147 / 139 | Human-labelled prompts across 12 harm categories |
| ToxicChat 0124 test (CC-BY-NC-4.0) | 184 / 191 | Real user prompts to a chatbot, toxic or not (all toxic ones, non-toxic sampled) |
| Coding prompts (`harm_data.py`) | 0 / 20 (44 across dev and holdout) | Legitimate coding requests that sound harmful: sysadmin, security, moderation tooling, games, medicine and law |

The harmful prompts all come from the published sets, which are read at run time and not committed. Dev and holdout are split by a stable hash. The dev threshold for each model is the lowest one that flags at most 5% of dev safe prompts.

## Results (holdout, at the dev threshold)

| Model | Licence | AUC | Recall | Safe flagged | XSTest safe / Aegis safe / ToxicChat safe flagged | Coding prompts flagged (all 44) at 0.5 → at threshold | p50 / p95 (MPS) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **Qwen3Guard-Gen 4B** | Apache-2.0 | **0.96** | **0.82** | 0.04 | 0.03 / 0.11 / 0.01 | 10 → **0** | 128 / 214 ms |
| **Qwen3Guard-Gen 0.6B** | Apache-2.0 | 0.95 | 0.76 | 0.04 | 0.05 / 0.09 / 0.01 | 15 → 1 | **34 / 55 ms** |
| GLiGuard (300M) | Apache-2.0 | 0.94 | 0.65 | 0.03 | 0.02 / 0.05 / 0.01 | 18 → 2 | 21 / 52 ms |
| Granite Guardian 3.2 3B-A800M | Apache-2.0 | 0.92 | 0.54 | 0.05 | 0.08 / 0.07 / 0.01 | 22 → 2 | 302 / 362 ms |
| KoalaAI Text-Moderation | OpenRAIL | 0.74 | 0.34 | 0.04 | 0.05 / 0.06 / 0.03 | 2 → 2 | 13 / 46 ms |
| Granite Guardian HAP 125M | Apache-2.0 | 0.72 | 0.28 | 0.05 | 0.10 / 0.06 / 0.01 | 2 → 3 | 6 / 13 ms |

Qwen3Guard is scored as P(Unsafe) + P(Controversial). P(Unsafe) alone at 0.5 gives the 4B recall 0.77, with 3% of safe prompts flagged and no coding prompts.

### What the numbers say

- **Unlike prompt injection, local models handle this task well.** Four Apache-licensed models separate harmful from safe prompts well (AUC 0.92–0.96), and they run in tens to hundreds of milliseconds on a Mac.
- **No model can be used at its own verdict.** At 0.5 (the model's "unsafe" answer), the four LLM guards flag 15–33% of safe prompts and 10–22 of the 44 coding prompts: killing a process on port 8080, an analysis of an incident's malware sample, a CTF exploit, moderation test fixtures, a gun shop's inventory. A threshold calibrated on dev fixes most of that, at the cost of some recall.
- **Qwen3Guard-Gen 4B is the best model.** It catches 82% of harmful prompts, flags 3% of XSTest's safe-but-scary prompts and none of the 44 coding prompts. It is also Apache-2.0, so keepmum could ship it or offer it as a download with no licence question.
- **Qwen3Guard-Gen 0.6B is almost as good and much cheaper.** It catches 76% at 34 ms. The one coding prompt it flags is "Store firearm serial numbers for our licensed gun shop's inventory system".
- **GLiGuard works here, unlike on injection,** but its scores bunch at the top (its dev threshold rounds to 1.000), so tuning its threshold is coarse. It catches 65%.
- **Granite Guardian 3B** is slower and catches less. **KoalaAI and Granite HAP** are toxicity filters (hate, abuse, profanity). They miss most other harm categories, and two of their few hits are "kill the zombie processes" and "my cron job keeps dying".
- **Aegis's safe set is the hardest for every model** (up to 11% flagged). Many of its "safe" prompts are borderline topics (drugs, weapons, crime) where annotators disagree.

### Limits of this benchmark

- The coding set is small (44 prompts, one author), so 1 of 44 is noise. A larger set of real coding-agent prompts would settle the false-alarm rate.
- The public sets are chat prompts, not coding-agent turns, and the models may have been trained on similar data.
- ToxicChat is licensed for non-commercial use: it is used here for evaluation only and not redistributed.
- Peak RSS undercounts for models on MPS, because the weights live in GPU memory. In bf16, the 4B needs about 8 GB and the 0.6B about 1.2 GB.

## Quantized, as keepmum runs it (Q8 and Q4_K_M GGUFs in llama.cpp)

Same data and protocol, with llama-server (llama.cpp, Metal) instead of PyTorch: [`harm-20261104-004512.md`](harm-20261104-004512.md). That run's memory column includes llama-server's default 8 GB prompt cache (`--cache-ram`), which keepmum turns off; the memory below was measured with it off, after 300 prompts.

| Model | AUC | Recall | Safe flagged | XSTest safe flagged | Coding prompts flagged (all 44) | p50 | Memory |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **Qwen3Guard-Gen 0.6B Q8_0** | 0.95 | 0.76 | 0.04 | 0.05 | 1 | 28 ms | **1.1 GB** |
| Qwen3Guard-Gen 0.6B Q4_K_M | 0.95 | 0.72 | 0.04 | 0.05 | 1 | 29 ms | |
| **Qwen3Guard-Gen 4B Q8_0** | 0.96 | 0.82 | 0.04 | 0.03 | 0 | 150 ms | **4.9 GB** |
| Qwen3Guard-Gen 4B Q4_K_M | 0.96 | 0.82 | 0.03 | 0.02 | 0 | 157 ms | |

Q8 matches bf16 at both sizes, to the second decimal. Q4_K_M costs the 0.6B four points of recall and costs the 4B nothing, but at the 4B's size it saves 2 GB of download and almost nothing in speed. keepmum uses Q8_0 for both, with each one's own dev threshold: 0.970 (0.6B) and 0.981 (4B).

## Verdict

1. **A harm check is feasible as an opt-in feature, and its model can ship.** Use Qwen3Guard-Gen 0.6B by default (fast, small) with the 4B as the accurate option, both Apache-2.0. Use the calibrated threshold (about 0.97–0.98 on P(Unsafe) + P(Controversial)), never the model's own verdict.
2. **Scan only what the user typed**, never tool results, system prompts or the resent history. That is where an acceptable-use policy applies, and it keeps the cost to one short text per turn.
3. **Default to warn, not block.** Even the best model flags about 3% of safe-but-scary prompts, and providers already refuse truly harmful requests. A local check adds most value as a record (audit log, dashboard) for teams that need one.
4. **Runtime:** Qwen3Guard is a Qwen3 decoder, so it runs as a quantized GGUF in llama.cpp, the same ggml family as pf-serve, with no PyTorch. The Q8 GGUFs keep the bf16 numbers (above).

*Nov 4, 2026, later:* built as `keepmum guard harm warn|block` (`[guard] harm`, `harm_model`), with the Q8_0 GGUFs at pinned commits and these thresholds. See the README's "Optional checks".
