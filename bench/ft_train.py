"""Fine-tune Horizon-Labs Prompt Injection Guard Small on bench/data/ft (ft_data.py), on this Mac's GPU (MPS).

Starts from the model keepmum runs, at a lower learning rate than Horizon-Labs' own training (5e-5), so it
keeps what it knows. Batches hold documents of similar length, up to TOKENS tokens each, accumulated to
about 32k tokens per step (Horizon-Labs used 65k). Windows of 2,048 tokens, as keepmum checks.
Every EVAL_EVERY steps it reports validation AUC and saves the best; the model folder carries the original
tokenizer files (with their normalizer), licence and notice.

    uv run --group torch python bench/ft_train.py                    # bench/data/ft/model-<stamp>/
    uv run --group torch python bench/ft_train.py --epochs 1 --lr 1e-5
"""

from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FT = ROOT / "bench" / "data" / "ft"
BASE = "Horizon-Labs/prompt-injection-guard-small"
BASE_REV = "2efc2ed5abecd21eb45c2506d231a65d6e392b85"


def batches(lengths: list[int], tokens: int, rng: random.Random) -> list[list[int]]:
    """Indices grouped by length, each batch at most `tokens` padded tokens, in random order."""
    order = sorted(range(len(lengths)), key=lambda i: (lengths[i], rng.random()))
    out, cur, longest = [], [], 0
    for i in order:
        longest2 = max(longest, lengths[i])
        if cur and longest2 * (len(cur) + 1) > tokens:
            out.append(cur)
            cur, longest2 = [], lengths[i]
        cur.append(i)
        longest = longest2
    if cur:
        out.append(cur)
    rng.shuffle(out)
    return out


def main() -> None:
    import torch
    from huggingface_hub import snapshot_download
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lr", type=float, default=2e-5)
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--max-len", type=int, default=2048)
    ap.add_argument("--tokens", type=int, default=8192, help="padded tokens per micro-batch")
    ap.add_argument("--accum", type=int, default=4)
    ap.add_argument("--warmup", type=float, default=0.06)
    ap.add_argument("--wd", type=float, default=0.01)
    ap.add_argument("--eval-every", type=int, default=150)
    ap.add_argument("--seed", type=int, default=3)
    a = ap.parse_args()

    torch.manual_seed(a.seed)
    rng = random.Random(a.seed)
    src = Path(snapshot_download(BASE, revision=BASE_REV))
    tok = AutoTokenizer.from_pretrained(src)
    model = AutoModelForSequenceClassification.from_pretrained(src).to("mps")
    inj = {v: int(k) for k, v in model.config.id2label.items()}["INJECTION"]

    def load(name):
        recs = [json.loads(line) for line in (FT / f"{name}.jsonl").read_text().splitlines() if line.strip()]
        enc = tok([r["text"] for r in recs], truncation=True, max_length=a.max_len)["input_ids"]
        return recs, enc

    t0 = time.perf_counter()
    train, train_ids = load("train")
    val, val_ids = load("val")
    lengths = [len(x) for x in train_ids]
    print(f"tokenized {len(train)} train, {len(val)} val in {time.perf_counter() - t0:.0f} s; "
          f"{sum(lengths) / 1e6:.1f}M tokens, mean {sum(lengths) / len(lengths):.0f}", flush=True)

    def collate(ids_list, idx):
        n = max(len(ids_list[i]) for i in idx)
        pad = tok.pad_token_id
        x = torch.full((len(idx), n), pad, dtype=torch.long)
        m = torch.zeros((len(idx), n), dtype=torch.long)
        for row, i in enumerate(idx):
            x[row, :len(ids_list[i])] = torch.tensor(ids_list[i])
            m[row, :len(ids_list[i])] = 1
        return x.to("mps"), m.to("mps")

    @torch.no_grad()
    def evaluate():
        model.eval()
        scores = [0.0] * len(val)
        for idx in batches([len(x) for x in val_ids], a.tokens * 2, random.Random(0)):
            x, m = collate(val_ids, idx)
            p = torch.softmax(model(input_ids=x, attention_mask=m).logits.float(), -1)[:, inj].tolist()
            for i, s in zip(idx, p):
                scores[i] = s
        model.train()
        pos = [s for s, r in zip(scores, val) if r["label"] == 1]
        neg = [s for s, r in zip(scores, val) if r["label"] == 0]
        auc = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))
        acc = (sum(s >= 0.5 for s in pos) + sum(s < 0.5 for s in neg)) / len(val)
        by = {}
        for s, r in zip(scores, val):
            by.setdefault(r["source"], []).append((s >= 0.5) == bool(r["label"]))
        return auc, acc, {k: round(sum(v) / len(v), 3) for k, v in sorted(by.items())}

    epoch_batches = batches(lengths, a.tokens, rng)
    total = math.ceil(len(epoch_batches) * a.epochs / a.accum)
    warm = max(1, int(total * a.warmup))
    opt = torch.optim.AdamW(model.parameters(), lr=a.lr, weight_decay=a.wd)
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: (s + 1) / warm if s < warm else max(0.0, (total - s) / max(1, total - warm)))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = FT / f"model-{stamp}"
    log = {"args": vars(a), "base": f"{BASE}@{BASE_REV}", "log": []}

    def save(step, auc):
        out.mkdir(parents=True, exist_ok=True)
        model.save_pretrained(out)
        for f in ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "LICENSE", "NOTICE"):
            if (src / f).exists() and not (out / f).exists():
                shutil.copyfile(src / f, out / f)  # contents only: the cache's copies are read-only
        (out / "train_log.json").write_text(json.dumps(log, indent=1))
        print(f"  saved step {step} (val AUC {auc:.4f}) to {out}", flush=True)

    auc, acc, by = evaluate()
    print(f"start: val AUC {auc:.4f}, acc {acc:.3f}, {by}", flush=True)
    log["log"].append({"step": 0, "auc": auc, "acc": acc, "by_source": by})
    best, step, micro, t0 = -1.0, 0, 0, time.perf_counter()
    model.train()
    loss_sum, loss_n = 0.0, 0
    epoch = 0
    while step < total:
        if epoch:
            epoch_batches = batches(lengths, a.tokens, rng)
        for idx in epoch_batches:
            x, m = collate(train_ids, idx)
            y = torch.tensor([inj if train[i]["label"] else 1 - inj for i in idx], device="mps")
            loss = torch.nn.functional.cross_entropy(model(input_ids=x, attention_mask=m).logits.float(), y) / a.accum
            loss.backward()
            loss_sum, loss_n = loss_sum + loss.item() * a.accum, loss_n + 1
            micro += 1
            if micro % a.accum:
                continue
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            opt.zero_grad()
            step += 1
            if step % 25 == 0:
                el = time.perf_counter() - t0
                print(f"step {step}/{total} loss {loss_sum / loss_n:.4f} lr {sched.get_last_lr()[0]:.2e} "
                      f"{el / 60:.1f} min, about {el / step * (total - step) / 60:.0f} min left", flush=True)
                loss_sum, loss_n = 0.0, 0
            if step % a.eval_every == 0 or step == total:
                auc, acc, by = evaluate()
                log["log"].append({"step": step, "auc": auc, "acc": acc, "by_source": by})
                print(f"step {step}: val AUC {auc:.4f}, acc {acc:.3f}, {by}", flush=True)
                if auc >= best:
                    best = auc
                    save(step, auc)
            if step >= total:
                break
        epoch += 1
    (out / "train_log.json").write_text(json.dumps(log, indent=1))
    final = out.with_name(out.name + "-final")  # validation saturates, so the last step is kept too
    model.save_pretrained(final)
    for f in ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "LICENSE", "NOTICE"):
        if (src / f).exists() and not (final / f).exists():
            shutil.copyfile(src / f, final / f)
    print(f"done in {(time.perf_counter() - t0) / 60:.0f} min; best val AUC {best:.4f}; {out}")


if __name__ == "__main__":
    main()
