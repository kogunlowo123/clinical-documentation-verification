# ADR-0002: The only write is a signed DocumentReference with Provenance

- Status: Accepted
- Date: 2026-10-14

## Context

The note becomes part of the legal medical record. Verification findings may
suggest problem-list or medication-list updates, but changing those resources
automatically would turn a documentation assistant into an unsupervised
clinical actor.

## Decision

- Submission always ends in `pending_approval`, with the policy decision for
  `fhir:write:DocumentReference` recorded as `require_approval`.
- Approval is a separate call by a clinician whose OIDC token carries a
  prescriber role and a SMART `fhirUser` that resolves to a Practitioner.
  Agents are denied by policy regardless of scope. Critical findings or an
  inconsistent band require an override reason.
- After approval the service re-evaluates policy as itself (and through the
  identity fabric when configured) with `clinician_approved=true`, then writes
  one FHIR transaction: the DocumentReference (conditional create on the
  verification UUID, `docStatus=final`, authenticator = signer) and its
  Provenance (author, verifier, signature, assembling agent, sources).
- Suggested chart changes appear in the note's reconciliation section for the
  clinician to act on in the EHR.

## Consequences

- No autonomous chart modification is possible through any interface (REST,
  MCP, A2A).
- Retries are safe: a failed write leaves the verification `approved` and the
  conditional create prevents duplicates.
- Problem-list and medication-list reconciliation stays a human task in the
  EHR; the service makes it faster, not automatic.
