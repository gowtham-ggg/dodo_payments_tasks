# Task 1 — Deploy & Harden the Workload

## What I started with

`ledger-api` came in about as insecure as it gets: containers running as root,
a live Stripe key and a DB password sitting in plaintext right in the
Deployment spec, no dedicated ServiceAccount, and nothing stopping anyone
from applying that same insecure manifest again tomorrow. Given it's in PCI
scope, all of that had to go.

Everything below was actually run against a local kind cluster, not just
written and assumed to work — every claim has a matching screenshot or
evidence file in `evidence/`.

## Setup

- kind (Kubernetes v1.36.1), cluster name `dodo-ledger`
- A local Docker registry (`registry:2`) attached to kind's Docker network so
  I could push and sign images without needing a cloud account
- Sealed Secrets v0.27.1, Kyverno v1.18.1 (both via Helm), Cosign v2.4.1

## What I did

### 1. Captured the "before" state first
Before touching anything, I deployed the original manifests as-is so I'd have
real evidence of how bad it was, not just my word for it:
- `evidence/evidence-before-insecure-pod.yaml` — no `securityContext` block anywhere
- `kubectl exec ... id` came back `uid=0(root)`
- The Stripe key and DB password were sitting in plain text in the pod spec

### 2. Hardened the Deployment
`deploy/deployment.yaml` now runs every container with:
- `runAsNonRoot: true`, `runAsUser: 1000`, `seccompProfile: RuntimeDefault`
- `readOnlyRootFilesystem: true`, all capabilities dropped, no privilege escalation
- an `emptyDir` mounted at `/tmp` since the filesystem is read-only and the app needs somewhere to write
- resource requests/limits and liveness/readiness probes wired to the app's real `/health` endpoint

📸 `evidence/screenshots/01-all-pods-healthy.png` — all pods running
📸 `evidence/screenshots/02-ledger-api-reporting-api-nonroot-uid1000.png` — `uid=1000`, not root
📸 `evidence/screenshots/03-ledger-api-securitycontext.png` — the full securityContext applied and live

### 3. Gave it its own ServiceAccount and locked down RBAC
`ledger-api` no longer uses the default ServiceAccount — it has its own, with
`automountServiceAccountToken: false`. On top of that I added a Role scoped to
exactly `get`/`watch` on one named ConfigMap, nothing else.

📸 `evidence/screenshots/06-ledger-api-rbac-scoped.png` — can read its one ConfigMap, can't touch secrets or pods

### 4. Got the plaintext secret out of git — Sealed Secrets
Replaced the raw Secret with a `SealedSecret` (`deploy/sealed-secret.yaml`),
which is encrypted against the cluster's own key and safe to commit. I
double-checked it actually decrypts to the right values and that the
Secret is properly owned by the controller (not just a leftover manual one).

📸 `evidence/screenshots/04-secret-via-secretkeyref.png` — secret pulled via `secretKeyRef`, no plaintext in the pod spec
📸 `evidence/screenshots/05-sealedsecret-ownership.png` — `ownerReferences` confirms it's controller-managed

### 5. Kyverno guardrails — and a bug I found the hard way
Three enforcing ClusterPolicies:

| Policy | What it blocks |
|---|---|
| `disallow-root-containers` | any pod that doesn't explicitly set `runAsNonRoot: true` |
| `disallow-latest-tag` | `:latest` or untagged images |
| `verify-image-signature` | any image without a valid Cosign signature |

**Worth calling out honestly:** my first version of the root-check policy
used Kyverno's `=()` optional-field syntax, which only validates a field *if
it's present*. The original insecure manifest has no `securityContext` block
at all, so that policy just skipped it instead of failing — the insecure
deployment went straight through the first time I tested this. I caught it,
rewrote the policy to actually require the field rather than optionally
check it, and re-tested. That same fixed policy also caught a mistake I made
on the `reporting` neighbour (I'd set `runAsNonRoot` at the pod level but
forgot the container level), which is a good sign the enforcement is real and
not just decorative.

📸 `evidence/screenshots/07-kyverno-blocks-insecure-deployment.png` — the original insecure manifest, rejected outright
📸 `evidence/screenshots/08-kyverno-blocks-unsigned-image.png` — an unsigned `nginx:alpine` image, rejected
📸 `evidence/screenshots/09-kyverno-allows-signed-image.png` — the Cosign-signed `ledger-api` image, allowed
📸 `evidence/screenshots/12-all-kyverno-policies-ready.png` — all three policies live and ready

### 6. Hardened the neighbour service too
`reporting` (a small curl sidecar) got the same treatment — non-root, dropped
capabilities, read-only filesystem, resource limits, and an exec-based
liveness probe since it has no HTTP endpoint to check against.

## Bonus items

### Persona-based RBAC
Three separate ServiceAccounts/Roles — developer, operator, admin — all
scoped to the `payments` namespace only:

| Persona | Read pods | Change deployments | Read secrets | Cluster-wide access |
|---|---|---|---|---|
| developer | yes | no | no | — |
| operator | yes | yes | no | — |
| admin | yes | yes | yes | no |

Even the "admin" persona can't list secrets in `kube-system` or list cluster
nodes — it's a namespace admin, not a cluster admin.

📸 `evidence/screenshots/11-persona-rbac-three-way-contrast.png`

### Pod Security Standards (restricted)
Applied at the namespace level as a second, independent layer on top of
Kyverno. To prove it's genuinely separate and not just Kyverno again, I sent
a root `nginx:alpine` pod straight at the API server — it got rejected
directly by Kubernetes itself, with completely different error wording than
Kyverno uses.

📸 `evidence/screenshots/10-pss-blocks-root-independently.png`

## A few decisions worth explaining

- **Sealed Secrets over External Secrets/SOPS** — it was the fastest way to
  get a fully working, git-safe secret store without needing a cloud KMS. For
  an actual PCI-scope production system I'd lean toward External Secrets
  backed by a real KMS/HSM instead — the Sealed Secrets private key living on
  the cluster itself is a single point of failure I wouldn't want in
  production.
- **Cosign key-based signing instead of keyless** — my network was doing TLS
  interception that broke the public Rekor transparency log, so I signed
  with a local keypair and skipped tlog upload. For Task 2's real CI/CD
  pipeline I'm planning to use keyless signing through GitHub Actions OIDC
  instead, which is the stronger option when it's actually available.
- **RBAC scoped to what the app actually needs**, not just "less than admin."
  `ledger-api` gets `get`/`watch` on one specific ConfigMap because that's the
  only API interaction it would realistically ever need — not a broader
  namespace-read grant just to be safe.

## Evidence

Everything referenced above lives in `evidence/` — YAML dumps of before/after
pod state, the Kyverno rejection output, the RBAC test results, and the
screenshots listed inline above.

## What I'd still improve with more time

- Point the Kyverno signature policy at the real Cosign keyless identity from
  the Task 2 pipeline instead of a throwaway local key
- Think through Sealed Secrets key backup/rotation properly — right now it's
  a single controller instance with no HA story
- Add a default-deny NetworkPolicy at the namespace level — holding off on
  this until Task 3 so I can cover it together with the Istio mesh layer
  instead of splitting the network story across two write-ups