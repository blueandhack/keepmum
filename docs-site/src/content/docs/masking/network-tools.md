---
title: Tools that reach the network
description: What MCP servers, web search and web fetch receive in place of your values.
---

The model never knows: it writes the placeholder, and keepmum decides on your Mac what the tool really receives.

```sh
keepmum tools                          # MCP servers and web tools agents offered, and what each may receive
keepmum tools allow gmail EMAIL PERSON # Gmail may also have addresses and names you typed
keepmum tools set gmail contacts       # or: placeholders | own | default
keepmum tools trust gmail              # its tools changed: trust them as they are now
```

- **Values it gave you** (the default for MCP servers): a value that came back in a server's results may go back to that server, so a search can be followed by opening what it found. Another server never sees it.
- **Also contact details**, or chosen types: values of those types go to the server wherever they came from, such as an address you typed.
- **Placeholders only:** nothing real, the default for web search and web fetch, which pass what they get to the public internet (giving them more asks for `--yes`).

Whatever the setting, a network tool never gets a secret, never more than 5 real values in one call (a search needs one or two; dumping a contact list needs dozens), and nothing while one of its server's tools differs from the one keepmum first saw or you last trusted. A server that rewrites a tool's description, or another server taking its name, gets placeholders until `keepmum tools trust`.

The servers, a hash of each tool's definition, the first sentence of its description, and how often a call kept placeholders live in `~/.config/keepmum/tools.json`; what the servers say about themselves, never values. The Mac app lists them in Tools, each with what it says it does, puts a dot on the menu bar icon for a new or changed server, and suggests a change when calls kept placeholders a setting would let through. Settings that give a tool more are recorded in the [audit log](/docs/watch/audit/) as turning protection down.

The per-tool settings are `[restore]` in the [config file](/docs/reference/configuration/). Codex scripts that hand a value to a network tool themselves are covered in [Limitations](/docs/about/limitations/).
