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

## Verdict

*Nov 3, 2026, later:* keepmum now offers Shield Small as an opt-in download (`keepmum guard setup injection`): the licence is shown and non-commercial use confirmed first, the weights are pinned and checked, and the guard defaults to off. See the README's "Optional checks".

1. **Do not ship a blocking guard on a classifier now.** The only model accurate enough to block on cannot be redistributed. The best Apache model misses six of ten injected documents and nearly all exfiltration and tool-call attacks, which are the ones keepmum cares about.
2. **Keep the deterministic defense as the primary one.** keepmum's restore guards (web fetch, web search and shell commands to unknown hosts keep placeholders) stop the outcome an injection is after, sending a secret out, whether or not the injection is detected. A classifier would only ever be an early warning on top.
3. **If a guard is wanted, make it warn, not block, and keep the model optional.** Options, best first:
   - Ask ZeroLeaks about a licence for Shield Small. Its runtime (ONNX Runtime with int8 weights) fits keepmum without PyTorch.
   - Wolf Defender Small as a low-noise "possible injection" note on the dashboard, Apache-2.0, ONNX available. It rarely raises a false alarm, but it misses most attacks, and the UI must not suggest otherwise.
4. **Re-run when new checkpoints land.** `guard.py` takes any Hugging Face sequence classifier. Patronus says a Wolf retrain is in progress, and Prompt Guard 2 needs only a login.
