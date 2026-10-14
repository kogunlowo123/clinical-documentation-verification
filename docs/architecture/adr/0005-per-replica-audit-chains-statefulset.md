# ADR-0005: StatefulSet with per-replica signed audit chains

- Status: Accepted
- Date: 2026-10-14

## Context

The agentic-core audit log is a hash chain: each event includes the hash of
its predecessor and an Ed25519 signature. Several replicas appending to one
chain would need distributed locking on every request, and an audit write
failure must fail the action it records.

## Decision

- Each replica owns its chain (`/var/lib/clinical-documentation-verification/audit/<pod>.jsonl`)
  on its own persistent volume; the workload is a StatefulSet so volumes follow
  pod identity.
- A log shipper copies chains to WORM object storage (S3 Object Lock in
  compliance mode, immutable Azure Blob, retention-locked GCS) provisioned by
  Terraform; `verify_chain` validates any chain offline with the public key.
- Audit writes are synchronous; if one fails the request fails.
- Audit details hold identifiers, decisions and digests, never transcript or
  note content.

## Consequences

- Chains are verifiable independently per replica; ordering across replicas
  uses timestamps and correlation IDs.
- Scaling down keeps the volume of the removed replica until its chain is shipped.
