# Task 2 — Secure CI/CD Pipeline & Supply Chain

## What this covers

The goal here was to make security something the pipeline enforces, not
something we just promise to do. Every gate below actually ran for real —
locally first, then on GitHub Actions — and I kept both a failing run and a
passing run as evidence, because a pipeline that only ever shows green
screenshots doesn't prove anything.

## Gates

Three scanners, wired into GitHub Actions as real blocking steps:

- **gitleaks** — secrets scan
- **Semgrep** — SAST
- **Trivy** — image/dependency CVE scan

Full fail-policy reasoning (what hard-blocks, what warns, how unfixable CVEs
are handled) is in [`fail-policy.md`](./fail-policy.md).

### Local runs first

Before wiring anything into CI, I ran all three scanners locally against the
Task 1 repo to see what they'd actually find:

- gitleaks found 8 leaks — the Stripe key and DB password preserved in Task
  1's "before" evidence files. `evidence/gitleaks-report-local-run.json`
- Semgrep found 5 blocking findings in the real app code — insecure
  `yaml.load()`, two SSRF findings on the same `/fetch?url=` param, a root
  Dockerfile, and Flask bound to `0.0.0.0`. `evidence/semgrep-report.json`
- Trivy found 179 HIGH/CRITICAL CVEs (33 CRITICAL) in the `python:3.6-slim`
  base image — it's been EOL since 2021. `evidence/trivy-report.json`

Worth calling out: Trivy independently flagged `PyYAML 5.1` as CVE-2019-20477
(critical, RCE via `yaml.load`) — the exact same root cause Semgrep caught by
reading the code. Two different tools, two different angles, same bug. Good
sign that the gates aren't redundant with each other.

### Real CI runs — the gitleaks/evidence problem

The first time I pushed the pipeline, gitleaks correctly failed on the same
Stripe key sitting in the Task 1 evidence files. My first fix — excluding
those exact finding fingerprints one at a time — was the wrong approach: any
new file referencing the same secret (like the scan reports themselves)
generates new fingerprints, so I'd be chasing this forever. I replaced it
with a path-based allowlist (`.gitleaks.toml`) that excludes `evidence/`
directories entirely, since those are expected to contain preserved findings
by design. Re-ran it — gitleaks went green, Semgrep stayed correctly red
(real bugs in the real app), and the build/sign stage stayed correctly
skipped downstream. That contrast — one gate fixed without weakening it,
one gate still doing its job — is screenshotted in
`evidence/screenshots/02-gitleaks-fixed-semgrep-still-blocks.png`.

I made a deliberate choice not to "fix" the original `ledger-api` app just
to make CI green. Those SSRF and deserialization bugs are real, and leaving
the pipeline red against the unpatched app is more honest evidence that the
gate works than quietly patching around it.

### Proving the full happy path

Since the original app correctly never reaches the sign/attest stage, I
built a second, clearly-labeled copy at
[`ledger-api-patched/`](./ledger-api-patched/) with the actual fixes:
`yaml.safe_load()` instead of `yaml.load()`, a real allowlist + private-IP
check on `/fetch`, a non-root Dockerfile user, and bumped dependency
versions (`python:3.12-slim`, current Flask/Werkzeug/PyYAML/requests).

Two Semgrep SSRF findings remained even after the fix, because Semgrep's
taint tracking doesn't understand my custom allowlist/IP-check logic as
valid sanitization — it just sees `request.args` reaching `requests.get()`.
I suppressed those two specifically with inline `nosemgrep` comments that
explain why, right next to the validation code that actually handles it —
not a blanket exclusion. `app.py` in that folder shows this directly.

This patched copy runs through a second workflow,
[`ci-cd-patched-demo.yml`](../.github/workflows/ci-cd-patched-demo.yml),
and its most recent run passed all three jobs — build, Trivy scan, push to
GHCR, Cosign keyless sign (GitHub OIDC), SLSA provenance attestation, and
signature verification, all for real.
`evidence/screenshots/03-full-pipeline-success.png` and
`04-cosign-verify-output.png` show this.

One tooling gotcha worth documenting: Trivy's GitHub Action ignores the
`severity` filter when `format: sarif` is set, unless you also set
`limit-severities-for-sarif: true`. My local `trivy image --format table
--severity CRITICAL` run was clean, but the same logical scan failed in CI
until I added that flag — it was checking against all severities, not just
CRITICAL, despite the input I'd given it.

## GitOps — ArgoCD

Installed ArgoCD into the same kind cluster and pointed an Application at
`task1-hardening/deploy/` in this repo, with `selfHeal: true` and
`prune: true`.

**Installing ArgoCD itself got blocked by my own Task 1 Kyverno policies** —
`disallow-root-containers` is cluster-wide by default, and ArgoCD's stock
manifests don't explicitly declare `runAsNonRoot: true` even though the
containers are non-root in practice. Rather than weaken the policy, I scoped
`disallow-root-containers` and `disallow-latest-tag` to the `payments`
namespace specifically — those guardrails exist to protect the PCI-scope
app, not to govern unrelated cluster infrastructure.

**Drift detection found something real.** Right after the first sync, the
Application showed `OutOfSync` on the Deployment even though nothing had
manually changed. The diff (`evidence/screenshots/`) showed git had
`image: ...ledger-api:insecure` while the live cluster had
`image: ...ledger-api:insecure@sha256:...` — Kyverno's `verifyImages` policy
has `mutateDigest: true`, so it pins the live pod to the exact signed digest
on admission. That's genuinely better security (prevents tag-mutation
attacks) but it will never match git's tag-based reference. Added an
`ignoreDifferences` rule for that one field rather than disabling
digest-pinning — the drift is real but expected, and the Application is
correctly `Synced` on everything else.

**Self-heal proof:** scaled `ledger-api` down to 1 replica manually with
`kubectl scale`. This actually surfaced a second real bug — the Deployment
had been running on a single replica since before I tightened the
root-container policy, and its container-level `securityContext` was
missing `runAsNonRoot: true` (only the pod-level field was set — same class
of mistake I'd already caught once on the `reporting` neighbour in Task 1).
When ArgoCD tried to self-heal back to 3 replicas, Kyverno correctly
rejected the new pods. Fixed it in git (not with `kubectl edit` — that would
defeat the point of GitOps), pushed, and ArgoCD picked up the commit and
reconciled the cluster back to 3/3 healthy on its own.
`evidence/argocd-application-final-state.yaml` and
`evidence/screenshots/07-argocd-selfheal-complete.png` show the end state.

## What I'd do with more time

- Canary or blue-green rollout via ArgoCD Rollouts — didn't get to this;
  the current setup is a plain Deployment with `RollingUpdate`, not a
  progressive delivery strategy
- Point Task 1's Kyverno `verify-image-signature` policy at the real Cosign
  keyless identity from this pipeline instead of the local test key it
  currently uses
- Fold `ledger-api-patched` back into a real PR against the original app
  repo rather than keeping it as a standalone demo folder — right now it
  exists purely to prove the pipeline mechanics work end to end
- Move the ArgoCD `ignoreDifferences` reasoning into a more permanent
  comment in the Application manifest itself, so it's not just documented
  here