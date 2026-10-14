# Runbook: incident response

Severity follows the enterprise incident policy. Any suspected wrong write to
a patient chart is SEV-1 and involves the clinical safety officer.

## Elevated 5xx

1. `kubectl -n clinical-documentation logs statefulset/cdv-clinical-documentation-verification --since=15m | jq 'select(.level=="error")'`
2. Correlate with deploys (`helm history cdv`). Roll back if the error
   started with a release (rollback runbook).
3. `/readyz` 503 means the database is unreachable; check RDS events.

## FHIR outage

`CdvFhirUnavailable`. Submissions return 503 and persist nothing; clinicians
retry later. Confirm with the EHR team; check the SMART token endpoint
separately (`upstream_error` with 401 means the client key or registration
changed). No action on the service is needed unless the client key rotated.

## Chart write failures

`CdvChartWriteFailures`. Affected verifications stay `approved` with no write
result:

```sql
SELECT id, patient_id, updated_at FROM verifications WHERE status = 'approved';
```

After the EHR recovers, the signing clinician (or a support user with a
clinician token) calls approve again; the conditional create guarantees a
single DocumentReference. Do not write notes into the EHR by hand.

## Prompt injection

`CdvPromptInjectionDetected`. The content guard blocked transcript content
before the model saw it; the verification continued with rule-based
extraction and needs clinician review as usual. Review the audit event
(`verification.submit`) and, if repeated from one source agent, suspend that
agent in the identity fabric.

## Suspected wrong documentation written

1. Identify the verification (`DocumentReference.identifier` = `urn:uuid:<id>`).
2. Export its audit events (`resource = verification:<id>`) and the chain
   segment; verify with `verify_chain`.
3. The clinician of record amends the note in the EHR (`DocumentReference`
   with `relatesTo.code = replaces`); this service never deletes notes.
4. Preserve the verification row; file a safety event.

## Replicas down

`CdvNotReady`. `kubectl describe pod` for scheduling or volume errors; a
replica cannot start if its audit volume is unavailable (by design).
