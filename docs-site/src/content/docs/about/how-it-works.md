---
title: How it works
description: Detect, mask, restore, and fail closed.
---

```
Claude Code ─┐                            ┌──▶ Anthropic API
Codex       ─┤                            │
Antigravity ─┤                            │
Gemini CLI  ─┼─▶ keepmum (on your Mac) ───┼──▶ OpenAI API
pi          ─┤   detect → mask            │
opencode    ─┘                            └──▶ Google (Gemini API, Code Assist)
  real values  ◀── restore ◀───────────────── placeholders only
```

## 1. Detect

Two layers, cheapest first:

- 96 rules, plus entropy and checksum checks: provider keys (formats from gitleaks and TruffleHog), secrets in command-line flags, config files and plain sentences, and, with personal data on, emails, phone numbers, SSNs and dates of birth.
- [OpenAI Privacy Filter](https://huggingface.co/openai/privacy-filter), an open-weight token classifier, for secrets in context and, optionally, personal data such as names and addresses. It runs in pf-serve, a small native program on [privacy-filter.cpp](https://github.com/localai-org/privacy-filter.cpp) that uses the Mac's GPU through Metal. You can turn it off and run on the rules alone (`keepmum model off`, see [Rules only](/docs/masking/rules-only/)).

A deterministic refinement step then drops what the model gets wrong: UUIDs, hashes, request IDs, timestamps, paths, placeholders and documented example keys. File names with an extension (`guard-20261104-231405.md`, `bench/results/PHASE0.md`) are not taken for secrets by the entropy check or the model, and the model's emails without an `@` are dropped; a token whose format a rule knows is masked wherever it is, file name or not.

**Guesses and exact finds.** The model, the entropy check and the generic `password = …` and `--token …` rules guess: they find secrets no format names, and are wrong now and then on ids, hashes, images and patterns. Format rules (`ghp_…`, `sk_live_…`, `AKIA…`) and values keepmum masked before are exact. Two kinds of guess are set aside:

- in text the model wrote itself (its words, the commands and edits it asks for): any real value there came from a request, where keepmum masked it (it is masked again, exactly) or sent it already. A guess there only took the model's own grep patterns, hashes it quoted to compare and request ids it named for secrets;
- at a value the provider already has as it is: shown unmasked in a message an earlier request sent, or written by the model (a label its own script prints). Masking it now would change that message, and with it the prompt cache. What decides is what went out, not the text before masking: a value that went inside a wider masked one was not sent.

A guess never hides an exact find. Where both found a value, the exact find's span is masked: a guess that took `STRIPE_SECRET_KEY:<base64>` gives way to the decoded base64 alone, so the name is not masked, and setting a guess aside never leaves a value an exact find would have masked. Base64 of nothing secret (keepmum's own placeholders, a plain word) is no secret either.

Of the 13 false masks the live tests found on Nov 22-23, 2026, every one came from a guess, and nine were of one of those kinds; the rest are handled by the refinement above. What you type and what tools return are guessed at as before.

Output a command printed encoded is decoded and scanned too: a hex dump (`xxd`, `hexdump -C`, `od` in any layout), `od -c`, plain hex, hex bytes after a label or in a list (`bytes: 47 49 …`, `0x47, 0x49, …`, as scripts models write print them) and base64. A block whose decoded text holds a secret, or a value masked before, is masked whole, so `xxd .env` or `base64 < .env` cannot carry a key out in hex or in pieces. Only decoded text is scanned, and only by the rules and the values masked before, so a hash or an image is left alone.

## 2. Mask

Each value becomes a placeholder derived from a secret per-install key (HMAC-SHA256). The same value always gets the same placeholder, so the provider's prompt cache keeps working. You [choose what is masked](/docs/masking/what-gets-masked/): turn off a type (names, dates…), add your own patterns, and list exceptions. Values live only in memory and are cleared when keepmum exits.

## 3. Restore

Placeholders in the model's answers are swapped back on your Mac, including in streamed text and in tool inputs (file edits, shell commands). Each value goes back in the syntax around its placeholder: quoted for the shell in a command, escaped in a JSON file or in Codex's JavaScript tool calls, so `p@ss$word` or `c0rrect\horse` arrives exactly as it was. A placeholder the model lowercased in a file name is still put back; a command holding one it mistyped is not run, and the model is told which one it meant.

[Tools that reach the network](/docs/masking/network-tools/) get only what they need: an MCP server gets back the values it returned itself (Gmail can open the thread it found), web search, web fetch and image generation keep placeholders, and none of them ever gets a secret. The same holds when Codex calls an MCP server from inside one of its JavaScript tool calls, next to a command that gets the real value, and in opencode's Code Mode scripts, which reach MCP servers and the web (`fetch`) themselves. A placeholder inside a web address in the answer's text stays too, since showing the answer would load an image or a link from that address. A prompt-injected request cannot send your keys to the internet.

## 4. Fail closed

If scanning fails, the body is malformed or compressed in a way keepmum cannot read, a request targets an endpoint keepmum does not scan, or a client opens a WebSocket, the request is blocked and never sent raw.
