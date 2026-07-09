# CI/CD Gate Fail Policy

## gitleaks (secrets scan) — hard block, always
Any detected secret fails the build immediately. No severity tiers — a
leaked credential is a leaked credential. Our own local run found 8 leaks
in Task 1's evidence files (a preserved insecure baseline, not a live key),
which is exactly the kind of finding this gate exists to catch before it
reaches a real production repo.

## Semgrep (SAST) — hard block on ERROR-level findings
Findings tagged as blocking (ERROR severity) fail the build:
insecure deserialization, SSRF, missing Dockerfile USER. Lower-severity
findings (INFO/WARNING) are surfaced in the PR but don't block merge —
otherwise the gate becomes noise and gets ignored.

Our local run found 5 blocking findings, all real: insecure `yaml.load()`,
two SSRF findings on the same unvalidated `url` param, a root Dockerfile,
and Flask bound to `0.0.0.0`.

## Trivy (image/dependency CVE scan) — tiered
- CRITICAL with a fixed version available → hard block. No excuse to ship
  a known-critical CVE when the fix is one version bump away.
- CRITICAL or HIGH with no fix available yet → WARN, not block. Blocking
  on an unfixable CVE just freezes the pipeline with no path forward; the
  team gets notified and tracks it, but doesn't get stuck.
- HIGH with a fix available → WARN by default, escalated to block if the
  CVE has a known exploit (KEV-listed) or the image sits in the CDE.

Our local scan against the still-insecure `python:3.6-slim` base found
179 HIGH/CRITICAL CVEs (33 CRITICAL) — with this policy in place, that
image would never pass the pipeline, which is exactly the intended outcome.
We deliberately left the Dockerfile unpatched here so this evidence reflects
a genuine failing run rather than a staged pass, and documented what fixing
it would take rather than doing it quietly.

## Cosign signing / SLSA attestation — hard block if missing or invalid
No unsigned image is allowed to reach the deploy stage — this is enforced
both by the pipeline (won't push/deploy without a valid signature) and
independently by the Kyverno `verify-image-signature` policy in Task 1,
which rejects unsigned images at admission time regardless of how they got
into the registry. Two layers, same intent.
