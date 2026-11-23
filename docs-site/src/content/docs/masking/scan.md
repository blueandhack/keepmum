---
title: Scan files (pre-commit)
description: Scan files for secrets with the same detection, without printing a value.
---

```sh
keepmum scan path/to/file …    # prints file:line:col TYPE rule [N chars]; never prints the value; exits 1 on findings
```

It exits 1 when it finds something, so it can run as a pre-commit hook.
