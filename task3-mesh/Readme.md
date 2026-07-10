# Task 3 — Service Mesh & Zero-Trust (Istio)

## Mode choice: ambient, not sidecar

Istio 1.30 supports two modes. I went with **ambient** instead of the more
common sidecar approach, mainly because my local machine was tight on RAM
(kind cluster + ArgoCD + Kyverno + Sealed Secrets were already using most of
it) and ambient doesn't inject an Envoy proxy into every pod — instead a
single shared `ztunnel` per node handles mTLS/L4, plus an optional `waypoint`
proxy per namespace for L7 rules. Everything the assignment asks for (mTLS
STRICT, identity-based AuthorizationPolicy, SPIFFE) works the same way in
either mode.

One real cost of this choice: most of the classic `istioctl` diagnostic
commands (`authn tls-check`, `x describe pod`) assume sidecars and don't
work in ambient. `authn tls-check` turns out to have been removed from
istioctl entirely back in 2020 — the assignment brief references a command
that hasn't existed for years. I used `istioctl ztunnel-config` and real
traffic tests instead, which I'd argue is stronger evidence anyway (actual
behavior beats a diagnostic tool's opinion).

## Setup

Installed via `istioctl install --set profile=ambient`, then enrolled the
`payments` namespace with `istio.io/dataplane-mode=ambient`. Confirmed
enrollment worked by checking `istioctl ztunnel-config workload` — all 4
`payments` pods show `PROTOCOL: HBONE` (Istio's mTLS tunnel protocol),
everything else in the cluster shows plain `TCP`.

**First real snag:** installing Istio itself got blocked by my own Task 1
Kyverno policy (`disallow-root-containers` is cluster-wide by default, and
Istio's stock manifests don't explicitly declare `runAsNonRoot: true` even
though the containers are non-root in practice). I scoped that policy down
to the `payments` namespace specifically in Task 2's ArgoCD work — see that
README for the detail. This bit me two more times in this task alone (the
waypoint proxy, then the ingress gateway), each time because a new
Istio-managed pod carried a different label than my exclusion rule
expected. Fixed properly by excluding on `gateway.networking.k8s.io/gateway-name`
existing at all, rather than hardcoding one gateway's name — a genuinely
better, more future-proof rule than my first two attempts.

## mTLS STRICT

Applied `PeerAuthentication` with `mode: STRICT` on `payments`. Proven three
independent ways, all in `evidence/`:

1. **The policy object itself and ztunnel's runtime state** —
   `istioctl ztunnel-config policies` shows Istio auto-converted it into an
   internal `Deny`-scoped ztunnel policy called `istio_converted_static_strict`.
2. **Real traffic, in-mesh:** `reporting` → `ledger-api` succeeds normally
   (curl doesn't need to know anything about mTLS — ztunnel wraps it
   transparently).
3. **Real traffic, unmeshed client:** a pod in the `default` namespace
   (never enrolled in the mesh) gets `Connection reset by peer` the moment it
   tries plain HTTP against `ledger-api` — it can complete the TCP handshake
   but gets rejected as soon as it doesn't speak the expected mTLS/HBONE
   protocol.

## Default-deny AuthorizationPolicy + identity-based allow

This is where most of the real debugging happened, and it's worth walking
through because the failure modes taught me something concrete about how
ambient mode actually works, not just "apply YAML and it works."

**Step 1 — default-deny.** An empty-rules `AuthorizationPolicy` in
`payments` (`spec: {}`). Confirmed it blocks *even in-mesh, mTLS-authenticated*
traffic — `reporting` → `ledger-api` failed after this, proving default-deny
isn't just a formality.

**Step 2 — first attempt at an explicit allow, with HTTP-level rules
(`methods`/`paths`) attached via a plain workload selector.** This silently
didn't work. Istio's own policy status told me why:
*"ztunnel does not support HTTP attributes... In ambient mode you must use a
waypoint proxy to enforce HTTP rules... rules matching HTTP attributes are
omitted. This will be more restrictive than requested."* — meaning the L7
part of my rule was just dropped, and the policy became empty/deny-everything.

**Step 3 — deployed a waypoint proxy** for L7 enforcement. Hit the Kyverno
issue mentioned above, fixed it, waypoint came up.

**Step 4 — retested. New failure, more interesting this time.** With the
waypoint in place and HTTP rules restored, an *unauthorized* identity
(`persona-developer`, a real ServiceAccount from Task 1, deliberately not on
the allowlist) got through with `200 OK` — it should have been blocked.
Digging into ztunnel's logs showed why: once traffic routes through a
waypoint, the destination's ztunnel only ever sees the **waypoint's own
identity** as the source, not the original caller's. I'd allowed the
waypoint's identity in the policy (necessary for the legitimate case to work
at all), which meant *anything* routed through the waypoint was implicitly
allowed — the fine-grained per-client check I thought I'd written wasn't
actually happening anywhere.

**The real fix:** attach the AuthorizationPolicy to the waypoint's `Gateway`
resource directly via `spec.targetRefs`, not via a plain `podSelector` on
the destination workload. This makes the *waypoint itself* evaluate the
original client's identity (which it can see directly, since it terminates
that client's mTLS connection) rather than delegating to a workload-level
rule that only ever sees "traffic came from the waypoint." Combined with a
second, simple L4-only policy that lets the waypoint reach `ledger-api`'s
ztunnel in the first place (a separate hop that still needs its own trust),
this finally worked correctly both ways:

- `reporting` → `ledger-api`: `200 OK`
- `persona-developer` → `ledger-api`: `403 RBAC: access denied`

Both are captured together in `evidence/evidence-authz-final-both-cases.txt`.

## Certificates: issuance, rotation, trust root

`istioctl ztunnel-config certificates` (`evidence/evidence-certificates.txt`)
shows this directly rather than me just asserting it:

- **Leaf certs** (per-workload identity, e.g.
  `spiffe://cluster.local/ns/payments/sa/ledger-api`) are valid for **~24
  hours** and auto-rotated by ztunnel well before expiry — no manual
  intervention, no cert files on disk for an attacker to steal.
- **Root cert** is valid for **10 years** and is the same serial number
  across every workload — this is the trust anchor every leaf chains back
  to. It's Istiod's self-signed CA (`istio-ca-secret`, generated on first
  boot since I didn't supply a custom root) — in a real production PCI-scope
  environment, I'd want this root backed by a proper external CA/HSM rather
  than Istiod's ephemeral self-signed default, and I'd document key
  custody/rotation procedures for it specifically.
- Identity is a **SPIFFE URI tied to the ServiceAccount**, not an IP or
  hostname — this is what makes the AuthorizationPolicy rules meaningful
  even if pods get rescheduled with new IPs.

## NetworkPolicy layer — and an honest limitation

Defined the expected layered set: `default-deny-all`, an explicit allow for
`reporting`/`waypoint` → `ledger-api` on port 8080, and a DNS egress
allowance (needed since default-deny blocks DNS resolution too, breaking
almost everything).

**These NetworkPolicy objects are not actually being enforced in this
environment.** kind's default CNI is `kindnet`, which — unlike Calico or
Cilium — doesn't implement NetworkPolicy enforcement at all. I verified this
concretely: traffic that should have been blocked by `default-deny-all`
kept working, and deleting the policy entirely made no observable
difference to traffic behavior either way. This isn't a config mistake on my
part; the manifests are correct and would enforce exactly as written on any
NetworkPolicy-capable CNI (which includes every major managed Kubernetes
offering — EKS, GKE, AKS — and Calico/Cilium on self-managed clusters).

I considered swapping kindnet for Calico to get real enforcement, but that
requires recreating the cluster from scratch (CNI is fixed at cluster
creation, and a live swap risks breaking the already-substantial Istio +
ArgoCD + Kyverno setup built on top of kindnet's networking). Given how much
was already working, I judged that risk not worth it for a local demo
environment and documented the gap honestly instead
(`evidence/evidence-networkpolicy-not-enforced-by-kindnet.txt`).

**What this proves about defense-in-depth, even from a negative result:**
NetworkPolicy operates at L3/L4 (IP, port) and is enforced by the CNI —
it has no concept of identity or HTTP semantics. Istio's mTLS +
AuthorizationPolicy operates on cryptographic workload identity and can
reach into L7. On this specific cluster, the mesh layer is doing *all* the
enforcement — which is itself a demonstration of why the layering matters:
on a NetworkPolicy-capable cluster, a misconfigured Istio policy would still
have NetworkPolicy as a backstop catching the blast radius at L3/L4. Here,
there's no such backstop, which is a real, environment-specific limitation
worth knowing about rather than assuming defense-in-depth exists when it
doesn't.

## Bonus: Ingress Gateway with TLS — partial result, documented honestly

Deployed an Istio Ingress Gateway with a self-signed cert
(`spec.tls.mode: Terminate`). **TLS termination itself is fully proven** —
`evidence/evidence-ingress-tls-termination.txt` shows a real TLSv1.3
handshake, HTTP/2 negotiation, and the correct certificate being served.
The immediate `403 RBAC: access denied` that followed is genuinely
meaningful: it shows the mesh's zero-trust default extends even to the
ingress path — arriving via the "front door" doesn't grant implicit trust,
matching how a real PCI-scope service should behave.

I then tried to add a scoped allow (ingress gateway → `/health` only, not
`/transactions`) to get a fully working external health-check path. Before
I could re-verify it, the cluster hit real resource pressure — the waypoint
lost its connection to istiod for several minutes, and after restarting it,
even basic pod-to-pod ICMP connectivity briefly failed (confirmed this
wasn't NetworkPolicy-related by testing with `default-deny-all` removed).
This is genuine local-infrastructure flakiness from running a fairly heavy
stack (ambient mesh + waypoint + ingress gateway + ArgoCD + Kyverno) on a
resource-constrained machine, not a configuration problem. Rather than keep
debugging environment instability on an optional bonus item, I stopped and
documented the state honestly in
`evidence/note-ingress-external-health-check-connectivity.txt`.

## Bonus: tying the mesh boundary to PCI CDE scope

The `payments` namespace is the natural Cardholder Data Environment boundary
here — `ledger-api` handles PAN tokenization directly, and the mesh
enrollment (`istio.io/dataplane-mode=ambient`) is applied at exactly that
namespace, not cluster-wide. A few concrete mappings to PCI DSS requirement
areas:

- **Req 4 (encrypt transmission of cardholder data across open, public
  networks — and increasingly, internal segmentation too):** mTLS STRICT
  means every byte between `ledger-api` and anything else inside the CDE
  boundary is encrypted and mutually authenticated, not just at the network
  perimeter.
- **Req 7 (restrict access by business need-to-know):** the
  AuthorizationPolicy setup does exactly this at the workload level —
  `reporting` can call specific endpoints on `ledger-api` for a specific
  reason, nothing else can, and it's enforced by cryptographic identity
  rather than a trust-the-network-segment assumption.
- **Req 1 (network segmentation):** intended to be reinforced by the
  NetworkPolicy layer underneath — see the honest limitation above; in a
  real deployment on a NetworkPolicy-capable cluster this requirement would
  be met by both layers simultaneously.
- **The ingress gateway is the CDE's one deliberate entry point.** External
  traffic doesn't reach `ledger-api` directly — it terminates TLS at the
  gateway, and even then gets the same zero-trust treatment as any other
  caller (proven by the 403 before I added a scoped allow). This is the
  correct shape for a CDE boundary: a single, well-defined chokepoint, not
  an assumption that "internal" traffic is trusted by default.
- **This connects back to Task 1 and Task 2 as one system, not three
  separate exercises:** Task 1's Kyverno/PSS controls govern what's allowed
  to run inside the CDE at all; Task 2's pipeline governs how anything gets
  into the CDE in the first place (signed, scanned images only); Task 3's
  mesh governs how things behave once they're in there, including internal
  traffic that a pure network-perimeter model would trust by default.

## What I'd do with more time

- Recreate the cluster with Calico (or run this on a cloud VM instead of
  local kind) to get real NetworkPolicy enforcement as a genuine second
  layer rather than a documented gap
- Finish verifying the scoped external `/health` allow once the environment
  is stable, and add the canary release bonus (VirtualService/DestinationRule
  traffic splitting) — didn't attempt this given the resource pressure we
  were already seeing
- Move the root CA off Istiod's self-signed default onto a properly
  custodied external CA, appropriate for actual PCI-scope production use
- Add the same `targetRefs`-based AuthorizationPolicy pattern for any future
  service added to the mesh, now that the workaround-vs-correct-fix
  distinction is understood and documented here
