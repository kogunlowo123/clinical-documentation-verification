# Runbook: disaster recovery

| Component | RPO | RTO | Mechanism |
|---|---|---|---|
| PostgreSQL | 5 min | 1 h | Multi-AZ, automated backups (14 days), point-in-time restore |
| Audit chains | 0 for shipped segments | n/a | WORM bucket, cross-region replication per organizational policy |
| Signed notes | 0 | n/a | System of record is the EHR |
| Service | n/a | 30 min | Stateless replicas, Helm chart in OCI registry |

Region loss:

1. `terraform apply` in the recovery region with the same variables.
2. Restore the database from the latest snapshot or PITR copy; update the
   `clinical-documentation-verification-database-url` secret.
3. Recreate the FHIR client and audit key secrets from the secret manager
   replica (keys are never in Terraform state).
4. Install the chart; verify readiness and one end-to-end verification
   against the EHR's DR endpoint.

Verifications lost between the last backup and the outage are re-submitted
from the scribe system (A2A `message/send` with the same message ID is
idempotent only while the original row exists, so expect new IDs).
Notes already written are safe in the EHR.
