# Benchmark

Accuracy, latency and memory per detector, on synthetic labeled data. The runtime is pf-serve; the
other detectors are earlier candidates, kept here so the results in `results/` can be re-run.

```sh
uv run python bench/gen.py                                   # writes bench/data/*.jsonl (seed 7)
uv run python bench/run.py                                   # rules, rules+pf-serve, rules+pf-serve:pii
uv run python bench/run.py --detectors rules,rules+pf-serve  # a subset; --data, --out, --repeat, --limit
```

Held-out checks use new values and hand-written negatives never used for tuning:

```sh
uv run python bench/gen.py --seed 11 --negatives holdout4 --out /tmp/keepmum-holdout4
uv run python bench/run.py --data /tmp/keepmum-holdout4
```

Earlier sets: `--seed 8 --negatives holdout`, `--seed 9 --negatives holdout2`, `--seed 10 --negatives holdout3`, and `--seed 12 --negatives holdout5` (shaped like the personal-data, flag and prose rules). Each was written before its first run; `results/PFSERVE.md`, `results/PHASE0.md` and `results/RULES.md` say which have been tuned on since.

- `rules` runs the pipeline without a model, as keepmum does with `detect.model = false`; `rules:pii` adds personal data.
- `extra.jsonl` holds the formats added in Oct 2026: more provider keys, secrets in flags, prose and config files, and personal data the rules find. It is generated after the other files, so they stay as they were for a given seed.

- `secrets.jsonl`, `pii.jsonl`, `long.jsonl` and `extra.jsonl` are generated: random values in provider formats. They are not committed, because provider-shaped strings trip GitHub push protection even when fake.
- `negatives.jsonl` comes from the hand-written lists in `negatives.py`.
- `pf-serve` is found where keepmum finds it (`KEEPMUM_PF_SERVE` and `KEEPMUM_PF_MODEL` override): run `native/pf-serve/build.sh` and `keepmum setup` first.
- Detection runs offline (`HF_HUB_OFFLINE=1`); models must already be downloaded.

## Earlier candidates

Each needs its dependency group and its weights:

| Detectors | Setup |
| --- | --- |
| `pf` (MLX bf16), `pf-8bit`, `pf-nemotron` (`pf_mlx.py`) | `uv sync --group mlx`, then `snapshot_download` of `mlx-community/openai-privacy-filter-bf16` / `-8bit` |
| `pf-torch` (`pf_mlx.py`) | `uv sync --group torch`, then `snapshot_download('openai/privacy-filter')` |
| `afm-cli`, `afm-sdk` (`afm.py`) | macOS 27 with Apple Intelligence; `uv sync --group afm` for the SDK |
| `gliner2`, `gliner-nvidia`, `credsweeper`, `piiranha` (`alt.py`) | `uv pip install gliner gliner2 credsweeper` |
| LLM judges (`judge.py`) | `uv sync --group mlx --group afm`; the Qwen judge needs `mlx-community/Qwen3-4B-Instruct-2507-4bit` |

Pipelines combine detectors with `+`, for example `rules+pf-8bit`, with flags `:pii` (personal data on), `:raw` (no secret-span refinement) and `:rawpii` (no personal-data refinement).

## Prompt-injection guard (`guard.py`)

A separate benchmark for a possible guard on tool results: which local classifier can tell an
instruction planted in a web page, file, email or MCP reply from the ordinary instructions coding
agents read all day (CLAUDE.md, skills, runbooks, security writing that quotes attacks). Results and
the verdict: [`results/GUARD.md`](results/GUARD.md).

```sh
uv sync --group torch --group onnx && uv pip install gliner2
uv run python bench/guard_data.py                    # bench/data/guard-dev.jsonl and guard-holdout.jsonl
uv run --group torch python bench/guard.py           # gliguard, wolf-small, wolf, horizon-small, horizon-base, sentinel, vektor-v2, protectai-v2, shield-small
uv run --group torch python bench/guard.py --detectors wolf-small --device cpu
# Shield Small as a GGUF: rebuild its weights from the int8 ONNX, convert, and compare (SRC: its pinned files)
uv run --no-project --python 3.12 --with onnx --with "numpy~=2.2.6" --with safetensors --with "torch==2.11.0" \
    --with "transformers==4.57.6" --with "sentencepiece<0.3" --with "protobuf<5" python -I bench/shield_gguf.py SRC OUT --type q8_0
uv run --group torch --group onnx python bench/shield_parity.py SRC OUT/shield-small-s15e.F32.gguf OUT/shield-small-s15e.Q8_0.gguf
# Prompt Injection Guard Small (the injection check's model) as a GGUF: convert, and compare with the original
# (SRC: Horizon-Labs/prompt-injection-guard-small at its pinned revision; MMBERT: jhu-clsp/mmBERT-small's tokenizer.json)
uv run --no-project --python 3.12 --with "numpy~=2.2.6" --with safetensors --with "torch==2.11.0" \
    --with "transformers==4.57.6" --with "sentencepiece<0.3" --with "protobuf<5" python bench/horizon_gguf.py SRC MMBERT OUT --type q8_0
uv run --group torch python bench/horizon_parity.py SRC OUT/prompt-injection-guard-small-v2.3.F32.gguf OUT/prompt-injection-guard-small-v2.3.Q8_0.gguf
# Fine-tune it on coding-agent tool output (the injection check's model since v2.3-km1), on this Mac's GPU
uv run --group torch python bench/ft_data.py                 # bench/data/ft/{train,val}.jsonl, kept out of git
uv run --group torch python bench/ft_train.py                # bench/data/ft/model-<stamp>/, about 45 minutes
# ... then convert it (horizon_gguf.py --model injection-ft) and compare on held-out data only:
uv run --group torch python bench/guard_public.py run --heldout --detectors horizon-small-ft
uv run --group torch python bench/guard_levels.py OLD.json NEW.json   # sensitivity levels that flag as the old ones did
```

- The models must be in the Hugging Face cache first (`snapshot_download` of each repo in `DETECTORS`).
  Llama Prompt Guard 2 (`prompt-guard-22m`, `prompt-guard-86m`) is gated: accept its licence on Hugging Face and log in.
- Shield Small runs from its ONNX weights with the windowing its card describes; its repo's `classify.py` is not executed.
- Real benign files are added at run time and not committed: this repo's docs and source, and the README of every package in `.venv`.

### On public data (`guard_public.py`)

The same question on public datasets: false alarms measured on real coding-agent tool output, and catch rates on
attacks written by others. Data lands in `bench/data/guard-public/` (kept out of git), results in
`results/guard-public-<stamp>.md`.

```sh
uv run --group torch python bench/guard_public.py build             # every source; --only swe-chat,prodnull
uv run --group torch python bench/guard_public.py run               # keepmum,horizon-base,horizon-large,wolf-small,wolf
uv run --group torch python bench/guard_public.py run --rescore bench/results/guard-public-<stamp>.json
```

| Source | Role | Licence |
| --- | --- | --- |
| [SWE-chat](https://huggingface.co/datasets/SALT-NLP/SWE-chat) | benign: real Claude Code, Codex and Gemini CLI sessions | ODC-BY, **gated** |
| [SWE-Hero](https://huggingface.co/datasets/nvidia/SWE-Hero-openhands-trajectories) | benign: OpenHands on real repositories | CC BY 4.0 |
| [AgentDojo](https://github.com/ethz-spylab/agentdojo) v1.2 | both: ground-truth calls replayed, clean and with its attacks (no model called) | MIT |
| [InjecAgent](https://github.com/uiuc-kang-lab/InjecAgent) | injected tool responses | MIT |
| [LLMail-Inject](https://huggingface.co/datasets/microsoft/llmail-inject-challenge) phase 2 | attack emails written to beat defences | MIT |
| [CodeIPI](https://github.com/UKGovernmentBEIS/inspect_evals) | both: issues, code comments, READMEs, configs | MIT |
| [prodnull](https://huggingface.co/datasets/prodnull/prompt-injection-repo-dataset) | both: repository files | Apache-2.0, **gated** |

- Gated sources need their terms accepted on Hugging Face by the account `hf auth whoami` shows; `build` skips them otherwise.
- AgentDojo runs in its own environment (`uv run --no-project --with agentdojo==0.1.34`, by `guard_public_agentdojo.py`).
- The threshold is set to flag 1% of one half of the SWE-chat and SWE-Hero output; the other half gives the false-alarm rate. `keepmum` is the check as keepmum runs it (the GGUF in llama-server), also reported at each sensitivity level.
- Not independent of every model: Horizon-Labs trained on LLMail phase 1 (phase 2 is used here), and Wolf Defender's card reports AgentDojo results.

## Harm content (`harm.py`)

Which local model could flag harmful requests in user prompts without blocking ordinary coding
and security work. Results and the verdict: [`results/HARM.md`](results/HARM.md).

```sh
uv sync --group torch && uv pip install gliner2 gliclass pyarrow
uv run python bench/harm_data.py                     # bench/data/harm-coding.jsonl: benign prompts that sound harmful
uv run --group torch python bench/harm.py            # every model in DETECTORS but the GGUFs; --detectors a,b
# Content Safety Guard Small (the harm check's default) as a GGUF: convert, and compare with the original
uv run --no-project --python 3.12 --with "numpy~=2.2.6" --with safetensors --with "torch==2.11.0" \
    --with "transformers==4.57.6" --with "sentencepiece<0.3" --with "protobuf<5" python bench/horizon_gguf.py SRC MMBERT OUT --model safety
uv run --group torch python bench/safety_parity.py SRC OUT/content-safety-guard-small-v1.2.F32.gguf OUT/content-safety-guard-small-v1.2.Q8_0.gguf
```

- Harmful prompts come only from published, labelled sets, downloaded at run time and not committed: XSTest v2 and Aegis 2.0 (CC-BY-4.0), ToxicChat (CC-BY-NC-4.0, evaluation only).
- `harm_data.py` holds benign prompts only.
- Generative guards are scored from their verdict token's probability in one forward pass, not by generating text.
- The Q8_0 and Q4_K_M GGUFs (`qwen3guard-0.6b-q8`, `-0.6b-q4km`, `-4b-q8`, `-4b-q4km`) run in `llama-server` (`brew install llama.cpp`) and are not in the default set: pass them with `--detectors`.
