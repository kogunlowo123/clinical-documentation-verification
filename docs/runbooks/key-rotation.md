# Runbook: key rotation

## SMART backend-services client key (every 90 days, or on suspicion)

1. Generate a new ES384 key and register its public JWK in the EHR client
   registration alongside the current key (new `kid`).
2. Update the `clinical-documentation-verification-fhir-client-key` secret and
   `cdv.fhir.keyId`; `helm upgrade`. Pods restart through the config checksum.
3. Confirm writes succeed (`cdv_fhir_writes_total{outcome="written"}`), then
   remove the old public key from the EHR registration.

## Audit signing key (yearly)

1. Generate a new Ed25519 key, store it as
   `clinical-documentation-verification-audit-key-<new-kid>`, archive the
   public key with the audit evidence.
2. Set `secrets.auditKeyFile: audit-<new-kid>.pem`; `helm upgrade`.
3. New events carry the new `key_id`; `verify_chain` takes both public keys.

## Workload identity and fabric credential

Projected service-account tokens rotate automatically (10-minute expiry); the
service re-reads the token file on every credential refresh. No action needed.
