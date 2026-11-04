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
uv sync --group torch && uv pip install gliner2 onnxruntime
uv run python bench/guard_data.py                    # bench/data/guard-dev.jsonl and guard-holdout.jsonl
uv run --group torch python bench/guard.py           # gliguard, wolf-small, sentinel, vektor-v2, protectai-v2, shield-small
uv run --group torch python bench/guard.py --detectors wolf-small --device cpu
```

- The models must be in the Hugging Face cache first (`snapshot_download` of each repo in `DETECTORS`).
  Llama Prompt Guard 2 (`prompt-guard-22m`, `prompt-guard-86m`) is gated: accept its licence on Hugging Face and log in.
- Shield Small runs from its ONNX weights with the windowing its card describes; its repo's `classify.py` is not executed.
- Real benign files are added at run time and not committed: this repo's docs and source, and the README of every package in `.venv`.

## Harm content (`harm.py`)

Which local model could flag harmful requests in user prompts without blocking ordinary coding
and security work. Results and the verdict: [`results/HARM.md`](results/HARM.md).

```sh
uv sync --group torch && uv pip install gliner2 pyarrow
uv run python bench/harm_data.py                     # bench/data/harm-coding.jsonl: benign prompts that sound harmful
uv run --group torch python bench/harm.py            # gliguard, qwen3guard-0.6b/4b, granite-guardian-3b, granite-hap-125m, koala-moderation
```

- Harmful prompts come only from published, labelled sets, downloaded at run time and not committed: XSTest v2 and Aegis 2.0 (CC-BY-4.0), ToxicChat (CC-BY-NC-4.0, evaluation only).
- `harm_data.py` holds benign prompts only.
- Generative guards are scored from their verdict token's probability in one forward pass, not by generating text.
