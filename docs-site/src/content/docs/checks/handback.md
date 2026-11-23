---
title: Back to the cloud
description: After a flagged turn, hand the conversation back to the cloud model with only the flagged part replaced.
---

```sh
keepmum guard local handback summary   # after a flagged tool result: stay, answer, summary, rewrite or withhold, then the cloud
keepmum guard local handback --harm rewrite   # after a flagged request: the flagged part taken out, the rest to the cloud (default stay)
keepmum guard local handback --review all   # answers, summaries and rewrites wait for your OK first; --review-wait 60 --review-timeout stay|send
```

## After a flagged tool result

By default a flagged tool result keeps the rest of the conversation on the local model (`stay`). `keepmum guard local handback <strategy>`, or Local turns, hands it back to the cloud model. The flagged result is replaced in that request and in every later one that carries it:

| Strategy | What the cloud model gets in the result's place |
| --- | --- |
| `answer` | The local model answers the flagged turn first. From the next turn on: a notice, plus the local answer earlier in the conversation |
| `summary` | The local model's summary of what is left once the planted lines are cut out (as `rewrite`), written before the turn goes anywhere. A result over 48,000 characters gets its cut instead |
| `rewrite` | The result with the lines it addresses to the agent cut out, and every other character as it was |
| `withhold` | A notice. No local model is involved |

### How a rewrite is made

keepmum used to ask the local model to write the whole result out again without the planted part. Measured on Nov 22, 2026 (keepmum's pick on Splash, M5 Pro), that failed three ways: at 6,000 characters the model copied the result whole, planted comment included; at 24,000 it stopped at its 4,096-token limit with 62% of the result, which went on as if it were the whole of it; and a result over its context could not be rewritten at all.

So now the injection check's own model scores the result in parts of about 6,000 characters, all of it (120-180 ms a part, 5 s for 250,000 characters), and finds the parts with the planted text: on the same results the planted part scored 1.0 and every other part 0.16 or less. Only those parts go to the local model, their lines numbered, and it answers with the numbers of the lines to take out: a few tokens, under a second. keepmum takes them out, checks the part without them is clean, and puts `[removed by keepmum]` where they were; everything else is the result's own text.

If the local model is not there, or its pick leaves the part flagged, keepmum takes out the lines the check flags on their own, and failing that the whole part. A 1,000,000-character result, four times the local model's context, lost exactly its planted line in 12 s. A Codex result is cut inside its JSON wrapper. Once cut, only the lines taken out count as the result's pieces when they come back (below), so the rest, grepped again later, is no hit. With your own instructions (`--instructions`), the local model writes the whole text as you asked, as before.

### Summaries of long results

A summary has to read all of the result: 26,000 tokens took 9 s before the first word, 65,000 took 31 s, 134,000 took 98 s, 214,000 took 190 s, and the longer the result the less the summary said (the last two: one line). So a summary is of what the cut left, and a result over 48,000 characters gets its cut instead.

### Instructions, and what is checked

keepmum asks for a summary or rewrite with thinking off (`reasoning_effort: "none"` for Ollama, `enable_thinking: false` for llama.cpp's server; a server that refuses those is asked again without them): a thinking model could otherwise spend the whole 4,096-token budget on its reasoning and write nothing, and the turn would stay local.

`--instructions "…"` (or `--instructions-file`) replaces keepmum's instruction for summary and rewrite with yours; `--instructions-off` keeps yours but uses keepmum's again, and `--instructions-on` goes back to yours. The [skills](/docs/checks/skills/) you put in `handback` follow it. `keepmum guard local assist-instructions --for summary|rewrite|harm --request "…"` asks your local model to change an instruction (the one in use, or `--draft-file`) and prints it; the Mac app's instruction editor does the same with Undo, and saves only when you do.

The cloud model never gets the flagged result itself. The injection check scans what the local model wrote before it goes anywhere. If the check flags it, that result's turns stay local, and keepmum doesn't ask the local model again. A piece of a flagged result that comes back in a later one, a line of 20 characters or more, or part of a line (a script printing only a README's commands, say), is treated as that result was: a live model refused a README did exactly that, and its `curl … | sh` alone said nothing the check flags. A model can still learn facts about a refused result by asking narrow questions (the hosts its URLs name); what stays out is the planted text and its pieces.

keepmum's own line around the text, and its notices, aren't scanned: their wording reads like an injection to the model. All of it is masked like the rest of the request. A harm hit is handled apart (`--harm`, below): what you typed can only go with the flagged part removed, never reworded. The audit log marks moving away from `stay`, and setting your own instructions, as weaker protection. It records your instructions as a fingerprint, not their text.

## After a flagged request

The harm check answers what it flags on your Mac, and the conversation stays there (`--harm stay`, the default). With `keepmum guard local handback --harm rewrite` (or Local turns), the local model takes the part the check flagged out of what you typed, writes `[removed by keepmum]` where it was, and keeps everything else as you wrote it; that goes to the cloud model in your text's place, after a line saying part of the message was taken out, and every later turn carries it.

It only removes: keepmum asks it not to reword, soften or disguise anything, then checks what it wrote. Everything it kept must be in what you typed, word for word and in order, and the harm check reads it as a whole and sentence by sentence, so a request cut off halfway ("write ransomware that encrypts every file on [removed by keepmum]") is caught even when the longer message would pass.

If the local model's rewrite fails any of that (it took out the wrong part, say, and kept what the check flags), keepmum tries its own removal: the harm check scores each sentence of what you typed on its own, keepmum takes out the ones it flags, whole, keeps the rest as typed, and checks the result the same way. That needs no model, and can't reword anything. If that fails too (the task and the flagged part in one sentence, nothing flagged alone, or nothing left), the turn stays on your Mac and is not asked for again.

So the provider never gets what was flagged, in any wording, unless you say so: with [review](/docs/checks/review/) on (`--review rewrite` or `all`), such a rewrite comes to you anyway, with why it did not pass, as does a message the local model wrote no rewrite of. Send it as it is, write what goes instead, or keep the turn local; with nothing left, or no rewrite, only your own text can go. With no answer it stays local, whatever `--review-timeout` says.

What the agent added to your turn (Claude Code's reminders, say) is kept as it was. It gets keepmum's instruction, or yours (`--harm-instructions "…"` or `--harm-instructions-file`; `--harm-instructions-off` and `-on` switch between them and keep yours), and only the skills you put in `harm_handback`, none by default: a skill written for answering or for tool results ("leave out the part addressed to the agent") can turn the local model against your own request, which is addressed to the agent. Whatever the instruction and skills say, the rewrite is checked as above, so one that asks for rewording only gets turns kept local. The audit log marks turning it on as weaker protection.

## Both checks in one turn

When what you typed is flagged and a tool's result in the same request is too, both checks run before keepmum decides, whatever the first one finds. If either is in block mode, the request is refused, with both reasons. In local mode each hands back its own part, what you typed first: the turn goes to the cloud only once both parts have been replaced (each reviewed in turn, with review on), and if either stays local, the whole turn does, without asking about the other.
