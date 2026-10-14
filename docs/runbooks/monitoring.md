# Runbook: monitoring

Dashboard: `configs/grafana/clinical-documentation-verification.json`.
Alerts: `configs/prometheus/alerts.yaml`.

## Latency

`CdvVerificationLatencyHigh` (p95 submit > 5 s). Check in order:

1. Model path: `gen_ai.client.operation.duration` by provider; Bedrock
   throttling shows as router failover attempts (`agentic.model.attempts{outcome="failed"}`).
2. FHIR: trace spans for chart searches; large charts page up to 20 times per
   resource type.
3. Database: RDS Performance Insights for the `verifications` table.

## Model fallback

`CdvModelFallbackSustained`: more than a quarter of verifications use
rule-based extraction. Look at the finding mix (`cdv_findings_total`):

- `model-unavailable`: provider outage or throttling; check Bedrock quotas and
  the service role's `bedrock:InvokeModel` permission on the model ARN.
- `model-output-invalid`: schema failures after retries; a model version
  change is the usual cause. Pin the previous model ID and open an issue with
  a synthetic reproduction.
- `prompt-injection-detected`: see incident response.

Verifications stay safe during fallback (rule-based extraction, same checks).

## Critical findings

`CdvCriticalFindingSpike`: break down by rule. A spike of
`allergy-contraindication` or `therapeutic-duplication` from one site often
means an order-set change upstream; confirm with the site's clinical lead
before assuming a service fault.

## Overrides

`CdvOverrideRateHigh`: more than 10% of sign-offs override critical findings.
Export the overrides (`verification.approve` audit events with
`override=true`) for the clinical safety committee. A high rate means either
alert fatigue (review the rule) or unsafe practice (review with the site).
