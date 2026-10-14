# Threat model

## Assets

| Asset | Impact if compromised |
|---|---|
| Encounter transcripts and draft notes (PHI) | Privacy breach; regulatory reporting |
| The legal medical record (EHR write path) | Patient harm from false documentation; legal exposure |
| SMART client key | Chart read/write as this service |
| Audit signing key and chains | Loss of accountability |
| Policy, dose table and rules | Unsafe orders pass screening |
| Model access (Bedrock) | PHI exfiltration through prompts; cost abuse |

## Trust boundaries

1. Agents and clinicians → service API (REST, MCP, A2A).
2. Transcript content → model prompt (untrusted speech inside a trusted request).
3. Service → EHR FHIR API; service → identity fabric; service → IdP JWKS.
4. Service → Bedrock.
5. Service → PostgreSQL, secret store, audit storage.

## STRIDE

| ID | Threat | Category | Control | Test |
|---|---|---|---|---|
| T1 | Forged clinician token under the IdP issuer and kid | Spoofing | Signature verification against IdP JWKS | `tests/security::test_t1_forged_clinician_token` |
| T2 | `alg: none`, tampered role claims | Spoofing / elevation | Algorithm allow-list, signature covers claims | `test_t2_alg_none_and_tampered_claims` |
| T3 | Expired, wrong-audience or HMAC-confused tokens | Spoofing | `exp`, `aud`, issuer pinning, EdDSA-only for agent JWKS path | `test_t3_expired_and_wrong_audience_tokens` |
| T4 | Agent signs a note or writes to the chart | Elevation (excessive agency) | `deny-agent-signoff`; write only after clinician approval; service-level write policy | `test_t4_agent_cannot_sign_or_write`, `test_write_requires_service_permission` |
| T5 | Scribe or patient-portal identity signs | Elevation | Prescriber roles; `fhirUser` must be Practitioner | `test_t5_non_prescriber_and_patient_identity_cannot_sign` |
| T6 | Dangerous order signed without notice | Tampering (safety) | Critical findings require override reason; recorded | `test_t6_critical_findings_cannot_be_signed_silently` |
| T7 | Prompt injection spoken in the room | Tampering | Content guard on EXTERNAL-trust transcript, spotlighting, schema-only output, deterministic normalization, no write tools | `test_t7_prompt_injection_in_transcript_is_contained`, `test_prompt_injection_never_reaches_the_model` |
| T8 | Delegated agent submits PHI on behalf of another | Elevation | `deny-delegated-agent-submission`; fabric delegation chain | `test_t8_delegated_agent_cannot_submit_phi` |
| T9 | Idempotency-key replay across encounters | Tampering | Key bound to patient and encounter | `test_t9_idempotency_key_cannot_replay_another_encounter` |
| T10 | Oversized or malformed input, path tricks in IDs | DoS / tampering | Bounded sizes, FHIR id pattern, `extra=forbid` | `test_t10_input_bounds_and_injection_in_identifiers`, schemathesis contract test |
| T11 | Audit tampering, PHI in audit | Repudiation / disclosure | Signed hash chain; identifiers only | `test_t11_audit_chain_is_intact_and_holds_no_transcript` |
| T12 | Tokens leaked in errors or logs | Disclosure | OAuth-style error bodies; log scrubbing | `test_t12_tokens_and_phi_not_in_errors` |
| T13 | Model fabricates evidence citations | Tampering | Grounding rejects out-of-range or non-mentioning citations | `test_t13_out_of_range_citations_never_ground`, `TestGrounding` |
| T14 | FHIR paging link redirects the bearer token | Disclosure (SSRF) | Next links followed only on the configured origin | `test_paging_link_to_foreign_host_is_refused` |
| T15 | Upstream outage leaves partial state | DoS / integrity | Chart read before persisting; idempotent conditional create; optimistic locking | `tests/chaos` |
| T16 | Audit write fails silently | Repudiation | Synchronous audit; failure fails the request | `test_audit_failure_blocks_the_workflow` |

## MITRE ATT&CK (Enterprise)

| Technique | Mitigation |
|---|---|
| T1078 Valid Accounts | IdP/fabric verification, short-lived credentials, role and Practitioner checks |
| T1528 Steal Application Access Token | Paging origin pinning; SMART tokens cached in memory only; no tokens in logs |
| T1565.001 Stored Data Manipulation | Signed notes only after approval; Provenance; optimistic locking |
| T1530 Data from Cloud Storage | KMS encryption, private endpoints, WORM audit storage |
| T1562.008 Disable Cloud Logs | Audit failure fails closed; Object Lock compliance mode |

## MITRE ATLAS

| Technique | Mitigation |
|---|---|
| AML.T0051 LLM Prompt Injection (direct and indirect) | Content guard with EXTERNAL trust threshold, spotlighting, system prompt separation |
| AML.T0054 LLM Jailbreak | Schema-only output; out-of-schema answers are rejected and retried, never executed |
| AML.T0057 LLM Data Leakage | Identifier redaction before the model; classification ceiling restricts providers; output guard |
| AML.T0048 External Harms | No tool can write; clinician signature required |

## OWASP Top 10 for LLM Applications (2025)

| Risk | Control |
|---|---|
| LLM01 Prompt Injection | Guard, spotlighting, schema-constrained output, deterministic re-parsing |
| LLM02 Sensitive Information Disclosure | PII redaction, BAA-only providers via classification ceiling, PHI kept out of audit and logs |
| LLM03 Supply Chain | Pinned dependencies (`uv.lock`), pip-audit, SBOM, Grype, Trivy |
| LLM04 Data and Model Poisoning | No training on transcripts; reference table under code review |
| LLM05 Improper Output Handling | Pydantic validation, grounding, no model text executed or written without signature |
| LLM06 Excessive Agency | Read-only tools; approval-gated single write; policy deny for agent sign-off |
| LLM07 System Prompt Leakage | Prompt contains no secrets; output guard blocks credential material |
| LLM08 Vector and Embedding Weaknesses | Not applicable (no retrieval store) |
| LLM09 Misinformation | Grounding, chart reconciliation, rule cross-check, consistency band |
| LLM10 Unbounded Consumption | Max tokens, bounded retries, input size limits, cost metrics |

## OWASP API Security Top 10 (2023)

| Risk | Control |
|---|---|
| API1 BOLA | Authorization per verification and patient; policy on every read |
| API2 Broken Authentication | Signature, issuer, audience, expiry; introspection for agents |
| API3 Broken Object Property Level Authorization | `extra=forbid`; views exclude internal fields (idempotency key, version) |
| API4 Unrestricted Resource Consumption | Size limits, paging limits, HPA, ingress rate limiting |
| API5 Broken Function Level Authorization | Distinct actions for verify, read, approve, reject, validate |
| API6 Unrestricted Access to Sensitive Business Flows | Sign-off requires prescriber identity; override recorded |
| API7 SSRF | FHIR base URL from configuration; paging origin pinned |
| API8 Security Misconfiguration | Production refuses ephemeral keys and unauthenticated FHIR; Helm `required` |
| API9 Improper Inventory Management | OpenAPI and schemas generated and drift-checked in CI |
| API10 Unsafe Consumption of APIs | FHIR and fabric responses validated; malformed bodies rejected |

## Risk register

| ID | Risk | Likelihood | Impact | Owner | Treatment | Residual |
|---|---|---|---|---|---|---|
| R1 | Clinician signs without reading (automation bias) | Medium | High | Clinical safety | Bands, critical-finding override, override-rate alert, training | Medium |
| R2 | Reference table gap misses an unsafe dose | Medium | High | Pharmacy & therapeutics | `unknown-drug` warning, table governance, commercial table via `CDV_FORMULARY_FILE` | Medium |
| R3 | Model provider outage | Medium | Low | Platform | Router failover, rule-based fallback | Low |
| R4 | EHR FHIR outage | Medium | Medium | Integration | 503 without partial state, retry, alert | Low |
| R5 | SMART client key exfiltration | Low | High | Security | Secret manager + KMS, key outside Terraform, rotation runbook | Low |
| R6 | Speech-to-text errors propagate | High | Medium | Clinical safety | Citations to utterances; clinician reviews against transcript | Medium |
| R7 | Lexicon misses a problem phrasing | High | Low | Clinical informatics | Model path, agreement metric, lexicon reviews | Low |
