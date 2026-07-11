# Dodo Payments — Security & DevOps Engineer Assessment

## Summary

`ledger-api` started as a textbook insecure deployment — root containers,
plaintext secrets, no guardrails, no zero-trust, no CI security gates. This
repo covers hardening it end to end, building a real CI/CD pipeline with
enforced security gates, wiring up a zero-trust service mesh, and then
switching hats to attack the original app and prove the vulnerabilities
those earlier controls would have caught were real and exploitable.

Everything below actually ran — locally on a kind cluster plus real GitHub
Actions runs — not just written and assumed to work. Where I hit real
problems (and there were several genuinely interesting ones), I've
documented what broke, why, and how I fixed it, rather than only showing
the clean end state.

## Tasks

### [Task 1 — Deploy & Harden the Workload](./task1-hardening/Readme.md)
Non-root, read-only filesystem, dropped capabilities, resource limits,
least-privilege RBAC, Sealed Secrets, Kyverno admission guardrails (with a
real policy bug found and fixed), persona-based RBAC, Pod Security
Standards. Fully complete including both bonus items.

### [Task 2 — Secure CI/CD Pipeline & Supply Chain](./task2-cicd/readme.md)
GitHub Actions pipeline: gitleaks, Semgrep, Trivy as real blocking gates,
Cosign keyless signing, SLSA attestation, ArgoCD GitOps with proven drift
detection and self-heal. Includes two real CI runs — one intentionally
blocked (proving the gates work), one fully green (proving the pipeline
mechanics work end to end). Fully complete including the RBAC/SARIF bonus
items.

### [Task 3 — Service Mesh & Zero-Trust (Istio)](./task3-mesh/Readme.md)
Istio ambient mesh, mTLS STRICT (proven three ways), default-deny
AuthorizationPolicy with identity-based (SPIFFE) access control — including
a real architectural bug found and fixed around waypoint identity handling.
Certificate issuance/rotation documented with live evidence. NetworkPolicy
layer defined correctly, with an honest documented limitation (kind's
default CNI doesn't enforce NetworkPolicy). Bonus: TLS-terminating Ingress
Gateway proven working; PCI CDE scope mapping ties all three tasks
together. Core requirements complete; canary release bonus not attempted
given time constraints.

### [Task 4 — Reconnaissance & Penetration Testing](./task4-recon-pentest/)
- [Part A: Recon](./task4-recon-pentest/recon/README.md) — passive OSINT
  against `dodopayments.tech` via certificate transparency logs, live-host
  fingerprinting, and TLS posture. 97 subdomains identified, 54 confirmed
  live, several genuinely notable exposures flagged (public ClickHouse HTTP
  interfaces on both dev and prod, publicly-reachable Keycloak admin
  console, broad internal-tooling exposure).
- [Part B: Pentest](./task4-recon-pentest/pentest/README.md) — real,
  working exploits against the authorized local target (`ledger-api`):
  unauthenticated cardholder data exposure (CVSS 7.5), SSRF (CVSS 9.3),
  and insecure deserialization leading to full remote code execution as
  root (CVSS 10.0). Every finding ties back to which of Task 1-3's controls
  would have mitigated it.

## A few things worth knowing before reading further

- **This is one system, not four separate exercises.** Task 4's RCE
  finding is the same vulnerability Task 2's Semgrep gate already flags in
  CI, and would be blast-radius-limited by Task 1's non-root hardening even
  if it were exploited. Task 3's AuthorizationPolicy model is the direct
  fix for Task 4's broken-access-control finding. I've tried to make those
  connections explicit rather than leaving each task as an island.
- **I made deliberate scope decisions under time pressure** (this was
  completed with limited time near the deadline) — most notably, Task 3's
  canary release bonus and a full re-verification of Task 4's exploits
  against the live mesh-enrolled deployment (rather than a standalone
  Docker container) were both cut. Each task's README documents exactly
  what was cut and why, rather than silently omitting it.
- **Several real, non-obvious bugs got found and fixed along the way** —
  a Kyverno policy that silently let insecure deployments through, an
  Istio ambient-mode identity-collapse issue that would have made the
  "zero-trust" claim false, a YAML duplicate-key bug, a Trivy CI action
  quirk. I've kept these in the write-ups rather than cleaning up the
  narrative, since I think how these got diagnosed and fixed is more
  informative than a report that only shows things working on the first
  try.

## Environment

Everything runs locally: kind (Kubernetes v1.36.1), no cloud account used.
GitHub Actions for the real CI/CD runs. Full tool list and setup steps are
in each task's own README.
