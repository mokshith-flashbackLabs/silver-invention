# Decision: liveness vendor for the enrolment gate

**Date:** 2026-08-05
**Status:** Accepted
**Deciders:** mokshith-flashbacklabs (with Claude analysis)

## Decision

**AWS Rekognition Face Liveness is the sole liveness verdict authority, on all client
platforms.** We do not build our own liveness detection — that option is permanently closed, not
deferred. Vendor alternatives (iProov) are a pre-agreed fallback with a concrete tripwire, defined
below, not a parallel track.

Two supporting decisions ride along:

1. **A free client-side preflight layer gates session creation.** Before the client asks the proxy
   for a liveness session (which costs money and burns one of the user's 5 attempts per 24h), it
   runs local checks — a face is present, exactly one face, lighting adequate — using on-device
   tooling (ML Kit / Vision framework on mobile, `FaceDetector` or equivalent on web). The preflight
   is **never** a liveness signal. It only decides whether starting a paid session is worth it.
2. **Challenge type is config, not code.** Rekognition offers `FaceMovementAndLightChallenge`
   (default, flashing colors, higher accuracy) and `FaceMovementChallenge` (no flash, faster,
   photosensitivity-safe). The session-create path reads the challenge type from config so an
   accessibility fallback is a config change, consistent with invariant 1b (one threshold/setting
   per purpose, from config).

## Context

Liveness is the enrolment gate: it proves a live human — not a held-up photo — is registering their
own face (invariant #2). Getting it wrong turns a victim-safety product into a stalking tool. The
client app is React Native, with a possible React web version. Services deploy to `us-east-1`.

## Options considered

| | Own-build | **AWS Face Liveness** | iProov | FaceTec |
|---|---|---|---|---|
| Per-check cost | huge fixed cost | **$0.015, tiering down** | enterprise quote | annual license |
| React Native | ours to build | **native bridge needed (~1–2 wks)** | official RN SDK | community wrappers only |
| React web | ours to build | **official component** | official SDK | official SDK |
| Anti-spoofing pedigree | none, uncertified | **strong, continuously updated** | gov-grade, deepfake focus | iBeta L1/L2 certified |
| Fit with repo spec | total rewrite | **spec already written around it** | moderate rework | moderate rework |
| Face-index coupling | n/a | **liveness yields `ReferenceImage` → `IndexFaces`, same vendor** | cross-vendor image handoff | cross-vendor image handoff |
| Biometric subprocessors | 0 (all liability ours) | **1 (AWS, already in stack)** | 2 | 2 |

**Own-build — rejected on the merits.** Presentation-attack detection is a specialist adversarial
ML field with an arms race against deepfake/injection attacks. Credibility requires independent
certification (ISO 30107-3 / iBeta). Months of R&D outside the team's competence, full liability in
a victim-safety product, competing against a 1.5-cent API call. Our moat is the monitoring
pipeline, not biometric anti-spoofing.

**FaceTec — eliminated.** Same React Native gap as AWS (no official RN SDK) plus an expensive
license on top. If a native bridge must be written anyway, bridge AWS's free SDKs.

**iProov — strong runner-up, kept as fallback.** Official `@iproov/react-native` SDK deletes the
bridge work entirely, and its anti-deepfake posture is arguably the industry's best. Costs: an
enterprise sales cycle and negotiated pricing, a second biometric subprocessor in a product whose
users are maximally sensitive about where their face goes, spec rework, and a cross-vendor handoff
of the verified enrolment image into the Rekognition collection.

## Why AWS wins

1. **Cost has no contest**: 10,000 enrolments ≈ $150, pay-as-you-go, no contract or minimum.
2. **The repo is already built around it**: NEAR-TERM-BUILD.md's session lifecycle, schema, and
   endpoints map 1:1 onto `CreateFaceLivenessSession` / `GetFaceLivenessSessionResults`.
3. **The liveness→index coupling**: a passed check hands back the `ReferenceImage` that goes
   straight into `IndexFaces` on the same vendor's collection. No cross-vendor image handling in
   the most sensitive data path.
4. **One biometric subprocessor, not two**: the privacy story names Amazon once.
5. **The one real weakness — no official React Native component — is bounded**: a one-off native
   module wrapping AWS's Swift/Kotlin SDKs, a well-trodden path (Expo Modules API, published worked
   examples), contractable if the team lacks native capacity.

## The tripwire (fallback trigger, agreed now)

When client-side liveness work starts, **timebox a React Native bridge spike to one week**. Success
= a real device completes a real Face Liveness check end-to-end through the bridged component. If
the spike fails, get an iProov quote and re-decide **then**, with evidence. Nothing in this repo
changes in that scenario — the service contract (session create / result / status) is
vendor-agnostic at the API level.

## Consequences

- Service-side build proceeds exactly as specified in NEAR-TERM-BUILD.md Part 1 (no changes needed).
- The client repo owns two new work items: the RN native bridge (spike first) and the preflight
  layer. The web React client, if built, uses `@aws-amplify/ui-react-liveness` as-is and doubles as
  the fastest real-AWS integration testbed.
- Web-originated liveness passes should be treated as weaker evidence than mobile (browser virtual
  camera injection is easier than defeating mobile SDKs) — matters when match-review tooling is
  built, not now.
- Exit cost is acknowledged: Rekognition face vectors cannot be exported, so leaving AWS later
  means every user re-enrols. Accepted; every vendor in the table has the same property.
- Config gains `LIVENESS_CHALLENGE_TYPE` when the liveness module is built (values:
  `FaceMovementAndLightChallenge` | `FaceMovementChallenge`).
