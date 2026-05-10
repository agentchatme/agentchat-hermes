---
name: Bug report
about: Something is broken or behaving unexpectedly
title: ""
labels: bug
---

<!-- Security issues: don't file here. Email security@agentchat.me. -->

### What happened

A clear, concise description of the bug.

### What you expected

What did you think should happen instead?

### Reproduction steps

```
1. ...
2. ...
3. ...
```

If the bug is in the wizard or CLI: paste the command + the output.
If the bug is in inbound message handling: paste the message that
triggered it (redact if it carries sensitive content).

### Environment

* `agentchatme-hermes` version (run `pip show agentchatme-hermes | grep Version`):
* `agentchatme` SDK version (`pip show agentchatme | grep Version`):
* Hermes Agent version (or commit hash if installed from main):
* Python version (`python --version`):
* OS:

### Logs

Paste the relevant log lines. The plugin logs under the
`agentchatme_hermes` logger; if you set `LOG_LEVEL=DEBUG`, include the
adapter and tools traces.

If your error response carried a `request_id`, include it — the
operator can correlate it with server-side logs.
