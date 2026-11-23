---
title: Observe only
description: See what keepmum would catch without changing any request.
---

```sh
keepmum observe on       # scan and count, but send every request unmasked
keepmum observe off      # mask again
```

:::caution
In observe mode every request goes to the provider unmasked.
:::

Observe mode shows what keepmum would catch without changing anything: requests go to the provider exactly as the agent sent them, nothing is blocked, and the dashboard says so on every page. Running proxies switch on their next request; if the config file becomes invalid, they mask.
