---
title: Review before it goes
description: Look at what the local model wrote, edit it, or keep the turn local before anything goes to the cloud.
---

```sh
keepmum guard local handback --review all   # answers, summaries and rewrites wait for your OK first; --review-wait 60 --review-timeout stay|send
keepmum review --watch               # answer them in this terminal (the Mac app shows a panel)
keepmum review                       # what waits
keepmum review ID send|withhold|local|edit --text-file F   # answer one
```

`keepmum guard local handback --review rewrite|all` (or Local turns › Review first) holds a rewrite, or any hand-back the local model wrote (answer, summary or rewrite), until you look at it.

## The review panel

The Mac app shows a floating panel with what the local model wrote, editable, beside the flagged result (for a rewrite, the lines it left out struck through), and a countdown. The panel doesn't take the keyboard until you click into it, so typing elsewhere carries on.

- **Send** sends it as written (⌘↩ once you've clicked in).
- **Send my edit** sends your text.
- **Withhold** sends a notice instead.
- **Keep local** keeps the turn on the local model.
- **Use the original** puts what the check flagged, unchanged, in the box, with a warning that the cloud model then gets the flagged part too; **Send the Original** takes two clicks, and sends the whole text as it was even when the box shows only its first 60,000 characters (`keepmum review ID original`).
- **Hide** puts the panel away without answering: the review waits in the menu bar until its time runs out.

Or edit it there, or **ask the local model** to change it ("keep only the install steps"): it gets the box and the original, and its text replaces the box, with Undo. While it writes the review is paused: the panel counts the pause up, the time left stands still and comes back after, with at least 30 seconds. Pauses add 2 minutes at most in all; by then the review ends and `--review-timeout` decides, even mid-draft.

A notification with the same answers appears too, and the menu bar icon shows a dot while a review waits.

### How long it waits

The agent waits meanwhile: with no answer in `--review-wait` seconds (60 by default, 10 to 600), the turn stays local (`--review-timeout stay`, the default) or goes as written (`send`).

And no review outlasts the agent waiting on it: keepmum ends it 10 seconds before that agent would give up on the request, so keepmum's answer still reaches it, and the panel shows how long the agent has waited (Claude Code 0:47 / 6:00). Measured against a server that never answers (Nov 15, 2026), Claude Code 2.1.310 gives up after 6 minutes (not the 10 its docs give), pi 1.5.0 and opencode 2.3.0 after 5, each then sending the request again; Codex CLI 0.169.0 waits on with no limit. What it wrote goes as your edit, checked like one.

### With `answer`

With `answer`, the local answer reaches the agent at once, and the review is of what comes next: your next message to the agent waits for it. The panel shows the local answer (the cloud model sees it as that turn's reply) and keepmum's notice, which goes in the result's place: **Send** hands the conversation back with the notice, **Send my edit** puts your text there instead (to pass on what the result said, say), and **Keep local** keeps every turn that carries the result on the local model.

### How long an answer holds

Your answer holds for that conversation: its later turns resend the result in their history, with your answer in its place, and are not asked about again. The same text new in a later request (typed again, or the same result returned again, in this conversation or another) is asked about again; only within 30 seconds of your answer is it reused, for the agent's own request sent with the turn (Codex asks for a task title with your prompt in it) or a retry, and the log says so.

### Rewrites and summaries

A rewrite is a cut, so the panel shows what it takes out, not the whole text: each run of lines struck through, with its line numbers and the line on either side, and how the check found it ("scored all 49 parts and flagged 1; qwen3.6 named the lines"); **Edit the text** opens the whole result to change it, when it is short enough to edit there.

A summary too long for the local model to read at once (more than its context) is not written; under review, the panel says so and offers keepmum's notice in its place instead, or your own text, and without review the turn stays local. The injection check scans your edit too, and the Logs page notes a flag, but your edit goes as you approved it.

## A whole turn, in one window

`--review turn` (Local turns › Review first › Every flagged turn, in one window) reviews each turn a check keeps on your Mac whole, whatever the hand-back settings, instead of one panel per change. A larger window lists every flagged piece of the turn, what you typed and each tool result.

![The turn window: every flagged piece of a turn, the original beside what goes in its place, and its own answer](../../../../../mac/docs/review-turn-light.png)

Each one shows the original beside what goes in its place, and takes its own answer:

- **Rewrite** (or, for a tool result, **Summary**), which the local model drafts while you look and keepmum checks as it would any hand-back (with keepmum's own sentence-by-sentence removal when the local model's rewrite of what you typed fails), and which you can edit, **Write Again** (asked afresh, so it can come out differently), or ask the local model to change;
- **Keep original**, with a warning;
- or **Remove** (keepmum's notice in its place).

Then **Send to the Cloud** (two clicks when any piece goes unchanged) or **Keep the Turn on This Mac**.

Your hand-back settings are the defaults, so a turn left alone does what they say: with `--review-timeout send` it goes only if every piece's default was written and passed its check, and otherwise stays local. The countdown pauses while the local model writes, as in the panel, up to 2 minutes in all. That conversation's later turns resend the pieces in their history, as you answered them; the same text new in a later request is asked about again. `keepmum review` lists a waiting turn, and `keepmum review ID send|local` answers it with the defaults. With nothing there to answer, a turn goes as the settings say, as it would without review.

## Try it

To see a review before one comes up, Local turns › See how it looks (or **Try a review** on the Protection map) opens a demo of the panel or the whole-turn window on made-up text: every answer works, and nothing is sent.

## When nothing is there to answer

keepmum holds a request only while something is there to answer: the Mac app asks every second, as does `keepmum review --watch` in a terminal (`keepmum review` lists what waits; `keepmum review ID send|withhold|local|edit --text-file F` answers one). With neither running, a hand-back goes without a pause. The audit log marks turning review down or off, and setting no answer to send, as weaker protection.
