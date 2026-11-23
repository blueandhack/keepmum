---
title: Sensitivity
description: How much the injection and harm checks flag, the second opinion, and what each level catches.
---

```sh
keepmum guard sensitivity injection low          # fewer false alarms: high, medium (0.222, the default), low, lowest, or a number
keepmum guard sensitivity harm low               # the harm check: high, medium (the default), low or lowest
keepmum guard confirm off                        # report every injection hit of the first model, without its second opinion (on by default)
```

## The injection check

The injection check flags a tool result scoring 0.222 or more (Medium). At Medium, Low and Lowest, one under 200 characters needs 0.40: short results (a grep line, an error, a script's message, the agent's own notices) raise most false alarms in real sessions, and few attacks are that short.

A tool result is scored as the agent would read it: the output inside Codex's wrapper, and a long run of base64 or hex as the text it encodes, or as a mark when it encodes none (a certificate, a key); text the model itself wrote earlier in the conversation, read back, is not scored.

If it flags too much, typically when you work on security code or read pages written for agents, raise the threshold (`keepmum guard sensitivity injection low|lowest|<number>`, 0.05–0.45, or Checks › Sensitivity). Each level flags as many real tool results as the same level did with the unmodified model, and a threshold saved for that model (0.65–0.98) becomes the level of the same name.

Two benchmarks: keepmum's test set ([bench/results/GUARD.md](https://github.com/blueandhack/keepmum/blob/main/bench/results/GUARD.md): 144 injected, 129 ordinary and 151 security documents), and 2,000 tool results from real Claude Code and Codex sessions in [SWE-chat](https://huggingface.co/datasets/SALT-NLP/SWE-chat) with attacks from AgentDojo and four other public sets (`bench/results/guard-public-*.md`):

| Level | Threshold (under 200 characters) | Injections caught | Ordinary documents flagged | Security documents flagged | Real tool results flagged | AgentDojo attacks caught |
|---|---|---|---|---|---|---|
| High | 0.185 | 97% | 2% | 4% | 2.6% | 92% |
| Medium (default) | 0.222 (0.40) | 97% | 2% | 3% | 1.4% | 91% |
| Low | 0.241 (0.40) | 97% | 2% | 3% | 0.9% | 91% |
| Lowest | 0.298 (0.40) | 97% | 2% | 3% | 0.4% | 91% |

AgentDojo is counted on its travel and workspace suites, which the model was not trained on. No level catches every attack: on the public sets, about half of those hidden in code files and repository docs get through. The check is a warning, not a guarantee.

### Long results, read whole

The model reads 2,048 tokens at a time, and keepmum scores every such window of a result, stopping at the first that flags. Until Nov 22, 2026 it scored 16 at most, the first 12 and the last 4, and a line planted halfway into a 250,000-character result went to the cloud unscored. Each window takes 100-180 ms on an M5 Pro: a clean 250,000-character result now adds about 5 s, once (a result scored before is not scored again), where 16 windows took about 2 s.

### Second opinion

A tool result the model flags is reported only if a second model agrees: [Wolf Defender Small](https://huggingface.co/patronus-studio/wolf-defender-prompt-injection-small) v2.1 by Patronus Protect (Apache-2.0, a [GGUF](https://huggingface.co/keepmum/wolf-defender-prompt-injection-small-gguf) keepmum hosts) must score it 0.001 or more. The two models flag different things, so together they raise far fewer false alarms: on 3,500 real Claude Code, Codex and OpenHands tool results, 53 flagged at Medium become 1, and none of the 25 files read (Read) that the model alone flagged. Catches stay within about two points (AgentDojo 93%, InjecAgent 73%, LLMail 97%, CodeIPI 40%), except malicious repository files (prodnull), 46% to 37% ([GUARD.md](https://github.com/blueandhack/keepmum/blob/main/bench/results/GUARD.md)).

The second model runs only on the first one's hits, in a llama-server of its own that starts on a hit and stops after 10 minutes unused: 0.4 GB for that while, and about half a second on the first hit in block mode while it starts. Until its model is downloaded (`keepmum guard setup injection`, or the service fetches it when it starts with the check on), every hit is reported, as with it off; if it fails, the hit stands too. `keepmum guard confirm off` (`[guard] injection_confirm = false`) reports every hit of the first model, and turning it on is audited as weakening protection. The levels in the table above are the model's alone: with the second opinion, each flags far less.

## The harm check

The harm check has the same four levels (`keepmum guard sensitivity harm …`, `[guard] harm_sensitivity`), with Medium as its default. Its models score on different scales, so it takes a level, not a number: each level flags about the same share of safe prompts on every model, and carries over when you change the model. On keepmum's harm test set ([bench/results/HARM.md](https://github.com/blueandhack/keepmum/blob/main/bench/results/HARM.md): 862 harmful and 994 safe prompts, 44 of them coding prompts that only sound harmful), harmful prompts caught / safe prompts flagged:

| Level | Small | 0.6B | 4B |
|---|---|---|---|
| High | 78% / 10% | 85% / 10% | 92% / 10% |
| Medium (default) | 66% / 5% | 76% / 5% | 82% / 5% |
| Low | 59% / 3% | 63% / 3% | 77% / 3% |
| Lowest | 48% / 2% | 56% / 2% | 56% / 2% |

Below Medium the harm check gives up real catches fast: for fewer false alarms at little cost, a bigger model at Low (4B) beats a smaller one at Medium.

## Changing the threshold

A text flagged under a lower threshold stops counting once its score is under the new one. Raising the threshold is audited as weakening protection. Results of Claude Code's AskUserQuestion, which hold your own answers, are not checked.
