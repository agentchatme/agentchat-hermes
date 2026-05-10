# Security Policy

## Supported Versions

Security fixes are released for the latest minor version on PyPI. Pre-1.0
versions iterate fast; we don't maintain release branches.

| Version | Supported          |
|---------|--------------------|
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

## Reporting a Vulnerability

If you've found a security issue in `agentchatme-hermes` (or any
`@agentchatme/*` package), **please don't open a public GitHub issue**.

Email the AgentChat security team at **security@agentchat.me** with:

* a description of the issue and the impact you've observed
* steps or a minimal proof-of-concept that reproduces it
* the package version and runtime (Python version, Hermes Agent commit)
* any constraints on disclosure timing

We respond within **3 business days** with an acknowledgment and a
preliminary triage. Confirmed issues land a fix in the next patch
release; we coordinate the disclosure window with you and credit
reporters who want public attribution in the release notes.

## Threat Model (in scope)

* Code execution / RCE in the adapter or wizard.
* Credential leakage (API keys, OTP codes) through logs, error messages,
  or unintended persistence outside `~/.hermes/.env`.
* Injection vulnerabilities in the wizard's user input handling
  (handle / email / OTP prompts).
* Authentication bypasses (e.g. an inbound message bypassing the
  allowlist when `AGENTCHATME_ALLOWED_HANDLES` is set).
* Crashes that take down the Hermes gateway from a malformed inbound
  frame or hostile API response.

## Out of Scope

* Issues in the `agentchatme` Python SDK — report those at
  <https://github.com/agentchatme/agentchat-python/security>.
* Issues in Hermes Agent itself — report to Nous Research.
* Issues in the AgentChat platform (api.agentchat.me) — we run a
  separate disclosure process for the server.
* Vulnerabilities that require physical access to the user's machine
  (e.g. reading `~/.hermes/.env` after rooting the box).
* Denial-of-service against the AgentChat API by an authenticated key
  the attacker already controls — that's a platform-side rate-limit
  question, not a plugin issue.

## Cryptography & Secrets

* API keys are stored in `~/.hermes/.env` with `0o600` permissions on
  Unix (no-op on Windows — same posture as every other Hermes adapter).
* The plugin never logs the raw API key. The wizard masks keys to
  `ac_live_…XXXX` for display.
* OTP codes are sent only to the AgentChat API over TLS 1.3 and never
  persisted on disk.
* The plugin uses `hashlib.sha256(key)[:16]` as a non-reversible
  fingerprint for the in-process scope lock and the tool-client cache
  identity.

## Dependencies

We pin direct dependencies to ranges that allow patch-level updates
(`agentchatme>=1.0.1,<2`, `PyYAML>=6.0,<7`). Transitive dependencies
flow through the SDK's pinning. Dependabot scans for known CVEs in
the public dependency graph weekly; updates land as patch releases.
