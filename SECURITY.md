# Security Policy

## Supported Versions

Security fixes are applied to the current `main` branch and the latest tagged release.

Older tags may not receive backported fixes.

## Reporting a Vulnerability

Do not open a public GitHub issue for a suspected security vulnerability.

Report it privately to the maintainer with:

- a clear description of the issue
- affected version or commit
- reproduction steps or proof of concept
- impact assessment if known

If you already have a private contact path with the maintainer, use that. If not, open a minimal GitHub issue asking for a private reporting channel without disclosing the vulnerability details publicly.

## Disclosure Expectations

- Please allow reasonable time to investigate and patch before public disclosure.
- Coordinated disclosure is preferred.
- Reports that include clear reproduction steps and concrete impact will be triaged faster.

## Security Boundary

FruitcakeAI is a local-first system. The current security posture is intended for operator-controlled deployments on trusted hardware and trusted networks unless additional hardening is added.

This repository does not currently claim:

- hardened internet exposure by default
- hostile multi-tenant isolation
- built-in TLS termination
- built-in rate limiting or WAF protections

For the current operator baseline, deployment checklist, known limitations, and endpoint sensitivity notes, see [Docs/SECURITY_BASELINE.md](Docs/SECURITY_BASELINE.md).
