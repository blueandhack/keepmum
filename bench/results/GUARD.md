# A prompt-injection guard for tool results: which local model

Nov 3, 2026 · Apple M5 Pro, 64 GB, macOS 27.2. Raw table and per-detector detail: [`guard-20261103-224732.md`](guard-20261103-224732.md) (scores in the `.json`). Harness: [`../guard.py`](../guard.py), data: [`../guard_data.py`](../guard_data.py).

## The question

Could keepmum screen what a coding agent reads (web pages, files, emails, MCP replies) for instructions planted by someone other than the user, and block or flag them? The guard only works if it does both of these:

- **Catch agent-directed injections**: "run this setup command", "send ~/.aws/credentials to …", "call send_email with …", often in polite prose with no "ignore previous instructions".
- **Stay quiet on the instructions agents read all day**: CLAUDE.md, skills, AGENTS.md, llms.txt, runbooks, install docs with `curl | sh`, and prompt templates in code. A guard that blocks a turn because the agent read a README is worse than no guard.

## Data

| | dev | holdout |
| --- | --- | --- |
| Injected documents (a payload in a carrier: start, middle, end, or a JSON field) | 96 (32 payloads × 3 carriers) | 48 (16 × 3) |
| Bare payloads (diagnostic only, not in recall) | 32 | 16 |
| Ordinary benign: the clean carriers, instruction-heavy files, package READMEs from `.venv` | 83 | 32 |
| Security-adjacent benign: writing that quotes attacks, keepmum's own docs and code | 64 | 29 |

The payloads and carriers are hand-written. Holdout payloads, carriers and negatives are disjoint from dev and were written before the first run. Real files are added at run time and are not committed.

## Results (holdout)

The threshold is set on dev so that it flags at most 2% of ordinary dev documents, then applied unchanged to holdout.

| Detector | Licence | AUC | Recall | Ordinary FPR | Security FPR | p50 / p95 per doc | Peak RSS |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Shield Small (ZeroLeaks, 118M, int8 ONNX, CPU) | **CC-BY-NC-4.0** | **0.97** | **0.90** | 0.03 | 0.17 | 20 / 286 ms | 0.8 GB |
| Wolf Defender Small (Patronus, 141M) | Apache-2.0 | 0.89 | 0.40 | **0.00** | **0.03** | 14 / 202 ms | 1.1 GB |
| Vektor-Guard v2 (ModernBERT-large) | Apache-2.0 | 0.67 | 0.15 | 0.06 | 0.10 | 45 / 962 ms | 0.7 GB |
| FunctionCallSentinel (ModernBERT-base) | Apache-2.0 | 0.65 | 0.04 | 0.00 | 0.17 | 21 / 278 ms | 0.6 GB |
| ProtectAI DeBERTa v2 (2024 baseline) | Apache-2.0 | 0.62 | 0.10 | 0.03 | 0.41 | 47 / 775 ms | 1.2 GB |
| **GLiGuard (fastino, 300M)** | Apache-2.0 | 0.55 | 0.04 | 0.06 | 0.14 | 59 / 1,133 ms | 2.7 GB |

Llama Prompt Guard 2 (22M, 86M) was not run: it is gated on Hugging Face, and the harness supports it once you log in.

### What the numbers say

- **GLiGuard is the wrong tool for this.** It is a content-safety moderator (harm categories, jailbreaks in user prompts). It does not separate a tool result carrying an injection from one that doesn't (AUC 0.55, about chance), and it is the slowest and largest model here. Its `unsafe` and attack-label scores are no better on their own.
- **Shield Small is the only model that works here.** It catches 90% of the injected documents, including the subtle, exfiltration and tool categories the others miss, and flags 3% of ordinary documents. It is also the right shape for keepmum: int8 ONNX on CPU, 20 ms per typical tool result, no PyTorch. **Its weights are non-commercial (CC-BY-NC-4.0)**, so keepmum cannot bundle it, and most of its users would be using it at work.
- **Wolf Defender Small is precise but misses most attacks.** It flags no ordinary documents and only 3% of security-adjacent ones. But it catches only the classic forms (override 0.83, multilingual and base64 1.00) and almost none of what matters for agents: exfiltration 0.08, tool calls 0.00, subtle 0.00, persistence 0.33. It flags 75–88% of the bare payloads but about 35–40% of the same payloads inside a document. Smaller windows (`wolf-small@256`, `@512`) did not change that: the misses come from what it learned, not from dilution.
- **The others flag too many benign documents.** Vektor, Sentinel and ProtectAI flag 16–31% of ordinary documents at 0.5; at a threshold that stops that, they catch almost nothing.
- **Security work is the expensive case for every model.** Even Shield flags 17% of keepmum's own source, tests and docs, and security writing that quotes attacks. Someone working on security tooling would see constant false alarms in block mode.

### Limits of this benchmark

The test set is small (48 injected and 61 benign documents in holdout), hand-written by one author, with static attacks. Wolf's own card reports 74–84% recall on AgentDojo tool outputs, so its low recall here may partly reflect how these payloads are phrased. Shield's training data includes agent tool results, which may resemble this set. Treat a gap of ±0.10 as noise; the gaps that matter here are larger.

## Shield Small as a GGUF in llama.cpp

*Nov 4, 2026.* Could the injection check run in llama.cpp, like the harm check, so that keepmum needs neither ONNX Runtime nor NumPy? Shield's release has only int8 ONNX weights. [`../shield_gguf.py`](../shield_gguf.py) rebuilds float weights from them (the values ONNX Runtime multiplies by) and converts them with llama.cpp's converter. The converter needed three fixes for this model:
- keep BERT's pooler, which Shield's classifier head needs
- read the XLM-R sentencepiece vocabulary
- give the output the width of the two labels

The Q8_0 file is published, unofficially and under the same licence, at [keepmum/shield-small-gguf](https://huggingface.co/keepmum/shield-small-gguf). [`../shield_parity.py`](../shield_parity.py) scores this benchmark's 409 documents with the same windows, five ways, the last one being keepmum's own runtime (`keepmum.guard.Shield`): [`shield-gguf-20261104-212908.md`](shield-gguf-20261104-212908.md).

| | Holdout AUC | Threshold (dev) | Recall | Ordinary / security FPR | p50 per document | Memory |
| --- | --- | --- | --- | --- | --- | --- |
| int8 ONNX, ONNX Runtime (CPU), keepmum's runtime before | 0.967 | 0.92 | 0.90 | 0.03 / 0.15 | 39 ms | 0.7 GB |
| Rebuilt weights, PyTorch fp32 | 0.968 | 0.946 | 0.88 | 0.03 / 0.12 | 31 ms | |
| **GGUF Q8_0 in llama-server (Metal), keepmum's runtime now** | 0.968 | 0.945 | 0.88 | 0.03 / 0.12 | **10 ms** | **0.4 GB** |

- **The conversion is right.** Given the same token ids, llama.cpp's logits match PyTorch on the rebuilt weights. The median difference in logit margin is 0.006 for F32 and 0.012 for Q8_0.
- **The remaining difference from ONNX comes from ONNX Runtime, not from llama.cpp.** ONNX Runtime also quantizes activations to int8 at run time, which moves the margin by a median of 0.04 (up to 2.2 on one window). PyTorch and llama.cpp both use float activations, so they agree with each other and differ from ONNX by the same amount.
- **The GGUF's scores sit slightly higher, so its dev threshold is 0.945** instead of 0.92. With each runtime at its own threshold, verdicts agree on 401 of 409 documents, and all eight that differ score between 0.92 and 0.945. Five are injections the GGUF now misses. Three are benign documents ONNX flagged and the GGUF doesn't: a ticket, a test file and keepmum's harm.py. On holdout, that comes to one injection fewer caught (42 of 48), the same 3% of ordinary documents flagged, and fewer security documents flagged (12% instead of 15%).
- **keepmum's tokens match Shield's on all 409 documents.** llama.cpp's sentencepiece tokenizer matches Shield's except at the end: for text that ends in whitespace, Shield's tokenizer emits a final `▁` and llama.cpp's doesn't, so keepmum appends it.
- The Q8_0 file is 133 MB, against the ONNX's 118 MB plus a 17 MB tokenizer. keepmum sends llama-server eight windows at a time, in one micro-batch.

*Nov 4, 2026, later:* keepmum's injection check now runs this GGUF in llama-server at 0.945, and no longer needs ONNX Runtime, tokenizers or NumPy.

## Newer Apache-2.0 models: Horizon-Labs and Wolf Defender

*Nov 4, 2026.* Three Apache-2.0 models released since the first run, chosen because a third-party evaluation of fifteen detectors on agent tool outputs ([arXiv 2610.03448](https://arxiv.org/abs/2610.03448)) ranks two of them first and second (AgentDojo and τ-bench, at 1% false alarms). Run alongside Shield and Wolf Small for comparison: [`guard-20261104-231405.md`](guard-20261104-231405.md). Each runs in 2,048-token windows in PyTorch on MPS; Shield as before.

| Detector | Size | AUC | Holdout @0.5: recall / ordinary / security | Dev threshold | Holdout @dev: recall / ordinary / security | Dev @dev: recall / security | p50 per doc |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **[Horizon-Labs guard small](https://huggingface.co/Horizon-Labs/prompt-injection-guard-small)** v2.3 (`9760ad5a`) | 141M | **1.00** | 1.00 / 0.03 / 0.06 | 0.646 | **1.00 / 0.03 / 0.06** | 0.99 / 0.07 | 15 ms |
| [Horizon-Labs guard base](https://huggingface.co/Horizon-Labs/prompt-injection-guard-base) v2.2 (`0fd61a8c`) | 308M | 1.00 | 1.00 / 0.00 / 0.09 | 0.203 | 1.00 / 0.09 / 0.42 | 0.96 / 0.26 | 23 ms |
| Shield Small (CC BY-NC 4.0), int8 ONNX | 118M | 0.97 | 0.94 / 0.03 / 0.24 | 0.923 | 0.90 / 0.03 / 0.18 | 0.80 / 0.24 | 20 ms |
| [Wolf Defender](https://huggingface.co/patronus-studio/wolf-defender-prompt-injection) v2 (`24e93077`) | 308M | 0.85 | 0.35 / 0.00 / 0.03 | 0.240 | 0.46 / 0.00 / 0.06 | 0.42 / 0.06 | 23 ms |
| Wolf Defender Small v2 (`e311bc56`) | 141M | 0.90 | 0.35 / 0.00 / 0.06 | 0.141 | 0.40 / 0.00 / 0.06 | 0.30 / 0.09 | 15 ms |

- **Horizon-Labs Small beats Shield on both splits, at a third of Shield's security false alarms.** On holdout it flags all 48 injected documents, including every tool-call, subtle and exfiltration attack; Shield misses five. On dev (96 injected) it misses one, a web-search result scored 0.134; Shield misses 19. Its false alarms on security writing and keepmum's own code are 6–7%, against Shield's 18–24%.
- **Its threshold is stable.** 0.5 and the dev threshold (0.646) give the same holdout verdicts. Horizon-Labs Base's dev threshold falls to 0.203, which flags 42% of security documents on holdout; at 0.5 it does about as well as Small (0.00 / 0.09).
- **Wolf Defender is the full-size model of Wolf Small and does no better here**: about four in ten injected documents caught, nearly none of the tool, subtle or delimiter attacks.
- **Not contamination.** Horizon Small's weights date from Oct 7 (v2.3) and Base's from Oct 2 (v2.2); this benchmark's data was written and first committed on Nov 3. Its training data does include planted-task documents and tool responses (rgeada/tool-response-injections, Nemotron agentic indirect injection), which is the shape of this set.
- **The licence suits keepmum.** Apache-2.0 with a LICENSE and NOTICE file; the base model, mmBERT-small, is MIT. The card states that the training data is permissively licensed and lists the datasets.
- **It fits keepmum's runtime:** see the next section.
- Llama Prompt Guard 2 is still not run: Meta has not granted this account access to the gated repos.

Same limits as above: 48 injected and 66 benign holdout documents, all by one author. A perfect score here means this set is too easy for Horizon Small, not that it catches every injection.

## Horizon-Labs Small as a GGUF in llama.cpp

*Nov 4, 2026, later.* keepmum's injection check now runs Prompt Injection Guard Small, converted to a GGUF by [`../horizon_gguf.py`](../horizon_gguf.py). Unlike Shield's release, Horizon-Labs' has float weights, so the conversion is llama.cpp's converter with four adjustments:
- read the tokenizer as mmBERT's. Horizon-Labs' tokenizer.json is mmBERT-small's plus a normalizer, which changes the converter's tokenizer fingerprint; the script checks that vocabulary, merges and pre-tokenizer are mmBERT's.
- write the pre-tokenizer in mmBERT's older form, which llama.cpp loads
- set rank pooling and an output as wide as the two labels
- write the name, licence and source into the GGUF

llama.cpp's tokenizer does not run the normalizer (decoding text smuggled in Unicode tag characters or variation selectors, NFKC, removing invisible characters), so keepmum runs it first (`keepmum.guard.normalize`). [`../horizon_parity.py`](../horizon_parity.py) scores the benchmark's 409 documents with keepmum's windows, three ways: [`horizon-gguf-20261104-234943.md`](horizon-gguf-20261104-234943.md).

| | Holdout AUC | Recall | Ordinary / security FPR | Dev: recall / security FPR | p50 per document |
| --- | --- | --- | --- | --- | --- |
| Original, transformers on MPS, at its dev threshold (0.6465) | 1.000 | 1.00 | 0.03 / 0.09 | 0.99 / 0.06 | 20 ms |
| **GGUF Q8_0 in llama-server (Metal), keepmum's runtime, at 0.65** | 1.000 | 1.00 | 0.03 / 0.09 | 0.99 / 0.06 | **12 ms** |

- **Verdicts match the original on all 409 documents**, for the F32 and the Q8_0 GGUF, given the same token ids or through keepmum's runtime. The largest score difference is 0.005 (F32) and 0.054 (Q8_0); the median difference in logit margin is 0.003 and 0.05.
- **keepmum's tokens match the model's tokenizer on all 409 documents**, and on hand-made obfuscated texts: tag-character and variation-selector smuggling, full-width letters, zero-width and bidi characters, CRLF, special-token strings.
- **Windows of 2,048 tokens give the fewest false alarms.** On holdout, the original in 512-, 1,024- and 2,048-token windows flags 18%, 9% and 6% of security documents, at the same recall ([`guard-20261104-233010.md`](guard-20261104-233010.md)). keepmum uses 2,048 with 256 tokens of overlap, at most 16 windows: 12 from the start, 4 from the end.
- **One window per request is the fastest and the smallest.** About 40 ms for a 6,000-character tool result, 100 ms for 16,000, 390 ms for 60,000; llama-server uses 0.4 GB (217 MB footprint). Four windows per request took 37% longer on the 60,000-character text, and 0.9 GB.
- **The threshold is 0.65**, the original's dev threshold rounded up. The Q8_0's own is 0.635; both give the same verdicts here.
- Security FPR is 9% here against 6% in the run above: keepmum's `doctor.py`, which now names the model, is one more holdout document flagged. The Q8_0 file is 164 MB, and the conversion is reproducible byte for byte.

## Verdict

*Nov 4, 2026:* superseded in part. Horizon-Labs Prompt Injection Guard Small (Apache-2.0) now beats Shield Small on this benchmark (above), so point 1's "the best Apache model misses six of ten" no longer holds. keepmum's injection check now uses it, as a GGUF in llama-server, and no longer offers Shield Small or asks users to confirm non-commercial use. Points 2 and 3 stand: the check is optional, off by default, and on top of the restore guards.

*Nov 3, 2026, later:* keepmum now offers Shield Small as an opt-in download (`keepmum guard setup injection`): the licence is shown and non-commercial use confirmed first, the weights are pinned and checked, and the guard defaults to off. See the README's "Optional checks".

1. **Do not ship a blocking guard on a classifier now.** The only model accurate enough to block on cannot be redistributed. The best Apache model misses six of ten injected documents and nearly all exfiltration and tool-call attacks, which are the ones keepmum cares about.
2. **Keep the deterministic defense as the primary one.** keepmum's restore guards (web fetch, web search and shell commands to unknown hosts keep placeholders) stop the outcome an injection is after, sending a secret out, whether or not the injection is detected. A classifier would only ever be an early warning on top.
3. **If a guard is wanted, make it warn, not block, and keep the model optional.** Options, best first:
   - Ask ZeroLeaks about a licence for Shield Small. Its runtime (ONNX Runtime with int8 weights) fits keepmum without PyTorch.
   - Wolf Defender Small as a low-noise "possible injection" note on the dashboard, Apache-2.0, ONNX available. It rarely raises a false alarm, but it misses most attacks, and the UI must not suggest otherwise.
4. **Re-run when new checkpoints land.** `guard.py` takes any Hugging Face sequence classifier. Patronus says a Wolf retrain is in progress, and Prompt Guard 2 needs only a login.
