# Agent specification

## Role and mission

- **Role:** clinical documentation verification agent.
- **Mission:** make sure that what a clinician signs matches what was said in
  the encounter, agrees with the chart or explains why not, and contains no
  unflagged medication-safety problem.
- **Business objective:** reduce documentation errors and review time for
  ambient-scribe notes without moving the signature away from the clinician.
- **Protected resources:** encounter transcripts (PHI), the patient chart, the
  legal medical record, audit evidence.
- **Operating boundaries:** reads the chart; writes only a `DocumentReference`
  and its `Provenance`, only after a clinician approves, only for the patient
  and encounter of the verification. Never places orders, never edits problem
  lists or medication lists, never diagnoses.

## Delegated scope

| Allowed | Disallowed | Risk | Escalation |
|---|---|---|---|
| Segment, extract and structure transcript content | Inventing facts not in the transcript (ungrounded items are flagged, never silently kept) | MODERATE | Ungrounded items shown as `UNVERIFIED` |
| Read Patient, Condition, MedicationRequest, AllergyIntolerance | Reading resources of other patients than the request | MODERATE | Authorization per patient |
| Screen orders and compute consistency | Blocking clinical judgment: findings inform, a clinician decides | MODERATE | Critical findings need an override reason |
| Write the signed note and Provenance after approval | Writing before approval, writing other resource types, approving | HIGH | Policy `require_approval`; `deny-agent-signoff` |
| Call Bedrock for extraction | Sending transcripts to non-BAA providers (classification ceiling 3 only on approved profiles) | HIGH | Router refuses ineligible providers |

## Objectives

| Type | Definition | Target |
|---|---|---|
| SLO availability | Non-5xx share of API requests, excluding upstream 503 | 99.9% over 30 days |
| SLO latency | p95 `POST /v1/verifications` (model path) | < 5 s |
| SLO latency | p95 reads and order validation | < 300 ms |
| SLI | `cdv:request_errors:ratio_5m`, latency histograms | Prometheus |
| KPI | Share of signed notes with zero unresolved critical findings | Tracked per site |
| KPI | Override rate on critical findings | Reviewed monthly by clinical safety; alert above 10% |
| KPI | Model fallback rate | < 5% steady state |
| Safety invariant | Chart writes without a clinician decision | 0 (enforced by policy and state machine) |
| Completion | A verification is complete when the signed note is in the chart (`written`) or a clinician rejected it | Audited |

## Permissions

| Mechanism | Use |
|---|---|
| RBAC | Clinician roles from the enterprise IdP: `physician`, `nurse-practitioner`, `physician-assistant` (sign), `registered-nurse`, `medical-scribe` (submit, read, check orders) |
| ABAC | `policies/documentation-verification.yaml`: subject kind, roles, scopes, delegation, SMART `fhirUser` type; resource band and critical-finding count; environment override reason and clinician approval |
| Agent identity | SPIFFE IDs from the agent identity fabric; scopes `documentation:verify`, `documentation:read`, `orders:validate` |
| OAuth 2.0 / OIDC | Clinician bearer tokens (issuer, audience, signature, expiry); RFC 7662 introspection and fabric `/v1/authorize` for agents; RFC 7523 jwt-bearer for the service's own credential |
| SMART on FHIR | Backend services (`client_credentials` + `private_key_jwt`, ES384/RS384) with system-level read scopes and write scopes for DocumentReference and Provenance only |
| Workload identity | IRSA / Azure Workload ID / GKE Workload Identity for Bedrock, audit storage and KMS; projected service-account token (audience `identity-fabric`) for the fabric |

## Inputs and outputs

| Contract | Schema |
|---|---|
| Submission | `schemas/submit-verification.request.schema.json`: patient and encounter IDs (FHIR id syntax), LOINC note type, transcript as labelled text (≤ 200,000 chars) or diarized utterances (≤ 5,000), proposed orders (≤ 100) |
| Verification | `schemas/verification.response.schema.json`: segmented transcript, extraction with citations, chart diff, findings, consistency report, draft note, write policy decision, clinician decision, write result |
| Sign-off | `approve-verification.request`, `reject-verification.request` |
| Order checks | `validate-orders.request`, `order-validation.response`, `medication-sig.response` |
| Model output | `model-extraction.output`: the only shape the model may answer with |

### Validation

- All request bodies reject unknown fields; identifiers are constrained to FHIR
  id syntax; sizes are bounded.
- Model output must validate against `ModelExtraction`; invalid output is
  returned to the model with the validation errors, at most
  `CDV_EXTRACTION_MAX_ATTEMPTS` times, then the rule-based extraction is used.
- Model claims are re-parsed by the deterministic sig parser and lexicons, and
  grounded against the cited utterances.

### Provenance

The FHIR `Provenance` written with each note records: the clinician as author,
verifier and signer (ISO/ASTM E1762 author's signature), this agent as
assembler (SPIFFE ID), the verification and transcript digest as derivation,
and every chart resource version read (`Condition/…/_history/n`) as source.
The `DocumentReference` carries the verification UUID as identifier
(conditional create), `docStatus=final`, the authenticating practitioner and
a restricted confidentiality label.

### Audit metadata

Every decision and state change is an agentic-core audit event: actor (clinician
subject or agent SPIFFE ID), action (`authorize:<action>`,
`verification.submit|approve|reject`, `fhir.write`, `mcp.tool.<name>`),
resource (`verification:<uuid>`), outcome, correlation ID, policy digest,
matched rules and IDs. Clinical content is never placed in audit details.
