---
title: Rules only, without the model
description: Scan with the rules alone, without the local model, and what that misses.
---

```sh
keepmum model off        # scan with the rules alone
keepmum model on         # the rules, then the local model (the default)
```

The rules alone are faster and need no model download, no model process and no model file in memory. They find secrets with a known shape (52 provider formats, private keys, JWTs), secrets under a telling name (`DB_PASSWORD=`, `--token`, "the API key is …"), long random strings, and, with personal data on, emails, phone numbers, card and bank numbers, SSNs and dates of birth.

They miss what only context reveals: names, addresses, and a secret with no shape and no telling name around it. On the benchmark, secret recall is 0.99 either way; personal-data recall is 0.77 with the rules alone and 1.00 with the model ([bench/results/RULES.md](https://github.com/blueandhack/keepmum/blob/main/bench/results/RULES.md)).

Running proxies switch on their next request, and the Mac app has the same switch in Masking › Detector.
