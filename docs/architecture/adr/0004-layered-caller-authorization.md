# ADR-0004: Two identity planes and combined authorization decisions

- Status: Accepted
- Date: 2026-10-14

## Context

Two kinds of callers reach the service: AI agents (ambient scribes,
orchestrators) and clinicians. Agents are governed by the enterprise agent
identity fabric; clinicians by the enterprise IdP. Clinical rules (who may
sign, when an override is needed) belong to this domain, not to the fabric.

## Decision

- Tokens are routed by their issuer, read without verification only to select
  a verifier: the clinician issuer is verified against the IdP JWKS; agent
  credentials are introspected at the fabric (RFC 7662) or, when no fabric URL
  is configured, verified locally against the fabric JWKS with issuer and
  audience pinned. Opaque tokens go to introspection. Anything else is
  rejected; there is no anonymous mode.
- Every action is evaluated by the local domain policy (agentic-core
  PolicyEngine). For fabric-authenticated agents the fabric's `/v1/authorize`
  decision (scopes, delegation chain, classification ceiling,
  `purpose_of_use=TREAT`) is combined with deny-overrides, then
  require-approval, then allow.
- The service authenticates to the fabric with its own projected workload
  token (jwt-bearer) to authorize its chart write.

## Consequences

- An agent needs both fabric authority and domain permission; a clinician
  needs both IdP identity and the right role and Practitioner identity.
- Fabric outage blocks agent traffic (fail closed) but not clinicians.
- Each decision is audited with the local policy digest and the fabric decision.
