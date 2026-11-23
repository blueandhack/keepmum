---
title: Local mode
description: Send a flagged request to a model on your Mac instead of the provider, and choose that model, its context and its speed.
---

**Local** (injection and harm) checks first, like block, and sends a flagged request to a model on your Mac instead of the provider. The agent gets its answer, and later requests that still carry the flagged text go there too, until you rewind or `/clear`.

The local model gets the request with its real values (it stays on your Mac) but never your API key, and no tools: a flagged request is the one most likely to carry instructions planted for the agent, and without tools the local model can only answer in text. A note in its instructions says why the turn came to it. Use a capable model (8B or larger): a small one may repeat a planted instruction as advice, or help with what the harm check flagged.

To hand the conversation back to the cloud model afterwards, see [Back to the cloud](/docs/checks/handback/).

```sh
keepmum guard local pull                             # downloads keepmum's pick to Ollama and uses it for local mode
keepmum guard local picks                            # the tested models, with their download and memory
keepmum guard local pull https://huggingface.co/owner/repo --quant Q4_K_M   # any GGUF on Hugging Face, or an Ollama name
keepmum guard local add --model qwen3:8b             # a model Ollama already has
keepmum guard local splash --start                   # Splash, first and in the background: about 1.6 times as fast with keepmum's pick (--stop stops it)
keepmum guard local add --preset lmstudio --model qwen/qwen3-14b --check harm   # another server, for one check
keepmum guard local                                  # the servers, in the order tried, and the models each has
keepmum guard local move 2 1 / set 1 --model … / remove 2
keepmum guard injection local        # a flagged request is answered by that model, never sent
keepmum guard local context 32k     # the Ollama or Splash model's context size: 16k, 32k, 64k (the default), 128k, up to the model's longest, or default
```

## Which model

`keepmum guard local pull` downloads keepmum's pick to Ollama and makes it the model for local mode, with a 64k context; the Mac app's Local models offers the same picks in each Ollama server's model menu. The picks are the models that did best in keepmum's tests of local mode, measured one at a time on an Apple silicon Mac:

| Pick | Model | Download | In use, 32k context | For a Mac with |
|---|---|---|---|---|
| `recommended` (default) | Qwen3.6 35B-A3B Uncensored (HauhauCS), IQ3_M | 16.3 GB | 16.1 GB | 32 GB or more |
| `larger` | the same, Q4_K_M | 22.1 GB | 21.7 GB | 64 GB or more |
| `light` | Qwen3.5 9B Defiant Fable (DavidAU), Q8_0 | 11.4 GB | 9.1 GB | 24 GB or more |
| `small` | the same, Q4_K_M | 7.7 GB | 5.9 GB | 16 GB or more |

The recommended one passed every test run, writes about 85 tokens a second on an M5 Pro, and uses about 2 CPU cores while it answers and none while idle. Any other model works too: a name from Ollama's library (`qwen3:8b`), or a GGUF repo on Hugging Face, by its link or as `hf.co/owner/repo:QUANT` (`--quant` picks the file; a link to one .gguf file names its own). Only Ollama downloads for keepmum: in LM Studio, download the model there, then choose it.

## Splash

[Splash](https://github.com/incoai/splash) (`brew install incoai/tap/splash`; an M3 or newer Mac on macOS 26.4 or later) is an engine built around a few models, with a draft model that guesses ahead. With keepmum's pick on an M5 Pro, it wrote 138 tokens a second against Ollama's 82, read an 8,000-token prompt in 2.3 s against 4.3 s, and used almost no CPU against Ollama's 2 cores; memory is about the same (19 GB with the 64k context). It passed keepmum's local-mode tests, and it answers Claude Code's and Codex's APIs as they are. It runs the `recommended` and `larger` picks, not the 9B ones.

`keepmum guard local splash --start` puts it first among the local servers, keeping the others behind it for when Splash is not running, and runs it in the background: a LaunchAgent (`~/Library/LaunchAgents/app.keepmum.splash.plist`) starts it now, again after a crash and at login, and logs to `~/Library/Logs/keepmum/splash.log`. The first start downloads the model and its draft, about 16.5 GB for keepmum's pick; `keepmum guard local` (and the app's row) shows how much has come, how fast, and how long is left, or that nothing has come for a while.

Hugging Face lets downloads go faster signed in: `keepmum guard local splash --hf-token` reads a token on stdin and saves it where Splash's downloader looks (`~/.cache/huggingface/token`, as `hf auth login` does), and Splash uses it from its next start. Do it first: a download that stops starts over.

Splash frees its memory after 5 minutes without a request (`--idle`), as Ollama does, and loads it again in about 4 seconds when one comes; started, it is ready in about 10. `--stop` stops it and removes the LaunchAgent. Without `--start`, keepmum prints the command to run it in a terminal yourself. In the Mac app, Add server › Splash, then Start on its row; Stop stops it.

## Context size

Ollama gives a model its longest context unless told otherwise, which can take several GB more (the recommended pick: 22.3 GB at its longest, 17.2 GB at 64k, 16.1 GB at 32k). keepmum sets it per model: it asks Ollama for a copy with that context (`keepmum/<model>:ctx64k`), which shares the downloaded files, so it takes no more disk. `pull --context` and `keepmum guard local context` choose 16k, 32k, 64k (keepmum's default), 128k, or `default` for Ollama's own setting; less takes less memory, and a long agent session needs more, since what does not fit is cut. The Mac app has the same choice in each Ollama server's Context menu. Ollama unloads a model after 5 minutes idle, so it takes memory only while flagged turns come in.

Splash takes its context when it starts (`--max-context`), so `keepmum guard local context` (or the Context menu on its row) starts the Splash keepmum runs again with the new one, ready in about 10 s, and keeps it for every later start (`[guard] local_splash_context`); `keepmum guard local splash --start --context 128k` chooses it from the start. Splash offers up to the model's own window, 256k for keepmum's picks (not past it: there Splash stretches the window, which changes every answer), and takes the memory only while a request that long runs: about 10 KB a token with keepmum's pick, whose attention is in 10 of its 40 layers, so 2.5 GB at 256k. A tool result longer than the context cannot be read locally: on an M5 Pro, 237,000 characters of source code was 64,500 tokens and took 31 s to summarise at 256k, past what a 64k context holds once keepmum's instructions are added.

## Speed

`keepmum guard local speed` (or Local models › Speed) sets these:

- `--keep-loaded 30m` (5m by default; 15m, 1h, any such duration, or `always`) is how long a server keeps its model loaded after a request: a reload takes seconds, and the next request is then read from scratch, where a loaded model reads only what is new (with keepmum's pick in Splash on an M5 Pro, a 37,000-token request took 14 s cold and 0.1 s from the cache). keepmum tells Ollama after each request, and starts the Splash it runs again with the new setting.
- `--thinking off` has a local answer come without the model's reasoning first: about 1.5 s instead of 7 s in the same test, where a hard turn may get a weaker answer. Summaries and rewrites never think.
- `--reads 32k` (the default; 16k, 64k, 128k, any size from 4k, or `full`) is how much of the conversation a local answer reads (`[guard] local_answer_context`): reading is most of its time (26,000 tokens took 9 s, 134,000 took 98 s), and a coding agent's conversation outgrows a local model's context, which the server then refused, and the agent got keepmum's refusal. Over it, the turn goes as Chat Completions with each message longer than 40% of it cut to its start and end, then the oldest messages left out, after the instructions and your first message, never the newest; a line says where. They go in steps of a quarter of it, so the conversation starts at the same message for several turns and a server's prompt cache reads only what is new. Never more than the server's own context (what it says of itself) less what the model writes; a request it still refuses as too long is cut again. A 440 KB Claude Code session was answered in 15 s, the local model reading 17,000 tokens.

## Servers

Where it goes is a list of local servers (`keepmum guard local add`), each a URL on this Mac and a model there, for both checks or one (`--check harm`: a bigger model for harm, say). A flagged request goes to the first that answers for its check: one that is not running, lacks its model or answers with an error is passed over. If none answers, or none is set, the request is refused, never sent, with a 403 that says to start the local model: agents retry a server error for minutes, and no retry helps until it runs.

Ollama (`--preset ollama`, the default), LM Studio (`lmstudio`) and llama.cpp's llama-server (`llamacpp`) speak the APIs Claude Code and Codex use, so the request goes as it is; a server that does not, such as `mlx_lm.server` (`mlx`), gets it as Chat Completions, earlier tool calls and results as text, and its answer goes back in the agent's API (`--api chat` asks that way from the start).

Every agent can go local. No local server speaks Gemini's API, so the Antigravity CLI's and Gemini CLI's flagged turns always go as Chat Completions. The answer comes back in Gemini's format: wrapped as Code Assist wraps it when you're signed in with Google, and streamed when the agent asked for a stream. Only a token count (`countTokens`) with a flagged result is still refused, since a local model can't count for it.
