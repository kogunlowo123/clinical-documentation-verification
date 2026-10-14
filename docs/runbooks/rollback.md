# Runbook: rollback

The schema is additive and versioned (`schema_version`); application
rollbacks within 0.x do not need database changes.

```bash
helm -n clinical-documentation history cdv
helm -n clinical-documentation rollback cdv <revision> --wait
kubectl -n clinical-documentation rollout status statefulset/cdv-clinical-documentation-verification
curl -sf <service>/readyz
```

Rolling back a model change: set `cdv.models.primaryModelId` to the previous
ID and upgrade; or set `cdv.models.enabled=false` to run rule-based only while
investigating. Rolling back a policy or reference-table change: revert the
commit and release; the policy digest in new audit events shows which version
decided.
