# Clinical Documentation Verification

Agentic verification of clinical encounter documentation. An ambient-scribe
transcript goes in; a checked draft note comes out, with every statement
cited to the utterance it came from, reconciled against the patient's FHIR
chart, medication orders screened for safety, and an explainable consistency
score. Nothing is written to the legal medical record until a clinician signs:
only then does the service write a `DocumentReference` and its `Provenance` in
one FHIR transaction.

```
Transcript ─▶ Clinical Extraction ─▶ Chart Review ─▶ Orders Validation
           ─▶ Consistency Analysis ─▶ Clinician Sign-off ─▶ Authorized Update
```

## Capabilities

| Stage | Implementation |
|---|---|
| Transcript segmentation | Speaker labels, timestamps and continuation lines; role resolution from label vocabulary, caller mapping, or cue-phrase scoring for anonymous diarization labels (`Speaker 1`); encounter sections (subjective, medications, objective, assessment/plan) |
| Clinical extraction | Rule-based extractor (formulary and SNOMED CT / ICD-10-CM / LOINC lexicons, NegEx-style assertion status, action cues, speaker rules: only clinicians order) runs on every transcript. When a model is configured, Amazon Bedrock extraction through the agentic-core `ModelRouter` with JSON-schema validation and corrective retries; output normalized by the same parsers and cross-checked against the rule-based result |
| Medication sig parsing | Dose and unit (mass conversion, tablet count x strength, ranges), route, dosage form, frequency (BID, q6h, q4-6h, at bedtime, weekly...), PRN and indication; The Joint Commission "Do Not Use" abbreviations flagged |
| Grounding | Every extracted item must cite utterances that mention it; unsupported model claims are kept, marked `UNVERIFIED` and penalized |
| Chart review | FHIR R4 `Condition`, `MedicationRequest`, `AllergyIntolerance` with bundle paging; items classified new / confirmed / changed / contradicted / discontinued; active chart medications not discussed are listed for reconciliation |
| Orders validation | Single and 24-hour dose ceilings (route-specific), route and form compatibility, duplicate orders, already-active medications, therapeutic duplication (anticoagulants, RAAS blockade, NSAIDs...), class interactions (opioid + benzodiazepine), allergy contraindication and cross-reactivity, controlled substances, incomplete orders |
| Consistency analysis | Weighted score over grounding, model/rule agreement, chart contradictions, order findings and order completeness; bands consistent / needs review / inconsistent; any critical finding forces inconsistent |
| Authorized update | Policy decision `require_approval` on every submission; approval by a physician, NP or PA identified by the SMART `fhirUser` claim; critical findings need an override reason; conditional-create transaction (idempotent retries); Provenance records author, verifier, signer, assembling agent, transcript and every chart resource version used |
| Agent interfaces | REST, MCP tools over streamable HTTP (`/mcp`), A2A Agent Card and JSON-RPC `message/send` / `tasks/get` |
| Identity | Clinicians: enterprise OIDC. Agents: [agent-identity-fabric](https://github.com/kogunlowo123/agent-identity-fabric) introspection and authorization, or local JWKS verification of fabric credentials; the service obtains its own fabric credential for the chart write |
| Evidence | agentic-core signed, hash-chained audit log for every decision and state change (no PHI in audit details) |

Shared controls (policy engine, audit chain, model router, content guard,
logging, telemetry) come from
[agentic-core](https://github.com/kogunlowo123/agentic-core).

## Quick start (local)

Requires Docker. No API keys or cloud credentials are needed: without a model
configuration the service runs rule-based extraction.

```bash
docker compose up --build
curl -s localhost:8080/readyz
curl -s localhost:8080/.well-known/agent-card.json | jq .skills
```

The compose stack runs PostgreSQL, a HAPI FHIR R4 server seeded with one
synthetic patient (`pat-1001`, see `deployments/docker/synthetic-chart.json`),
an OIDC provider for clinician tokens, and an OpenTelemetry collector.

## Development

```bash
uv sync
make check        # ruff, black, mypy --strict, bandit, contracts, tests (coverage >= 90%)
make integration  # PostgreSQL integration tests (needs CDV_TEST_DATABASE_URL)
make helm terraform
```

Test suites: `tests/unit`, `tests/security` (attacks mapped to the threat
model), `tests/chaos` (FHIR, database, fabric and audit failures),
`tests/performance` (latency budgets; `locustfile.py` for load), `tests/e2e`
(HTTP lifecycle, A2A, MCP and a property-based OpenAPI contract test),
`tests/integration` (PostgreSQL). External systems are exercised through
in-process fakes that speak their wire formats (`tests/fakes.py`); all
patient data in the repository is synthetic.

## API

| Endpoint | Caller | Purpose |
|---|---|---|
| `POST /v1/verifications` | Scribe agent, care team | Run the workflow; returns the draft note, findings, chart diff and score (`pending_approval`) |
| `GET /v1/verifications`, `GET /v1/verifications/{id}` | Care team, scoped agents | Review |
| `POST /v1/verifications/{id}/approve` | Physician, NP, PA | Sign; writes `DocumentReference` + `Provenance` |
| `POST /v1/verifications/{id}/reject` | Physician, NP, PA | Close without writing |
| `POST /v1/orders/validate` | Care team, scoped agents | Screen draft orders (optionally against the chart) |
| `POST /v1/medications/parse-sig` | Care team, scoped agents | Structure a medication instruction |
| `POST /mcp` | MCP clients | Tools `verify_clinical_documentation`, `get_verification`, `validate_orders`, `parse_medication_sig` |
| `GET /.well-known/agent-card.json`, `POST /a2a` | A2A agents | Agent Card; JSON-RPC `message/send`, `tasks/get`, `tasks/cancel` |
| `GET /healthz`, `/readyz`, `/metrics` | Platform | Operations |

Full contract: [docs/api/openapi.json](docs/api/openapi.json); JSON Schemas in [schemas/](schemas/).

## Deployment

| Layer | Location |
|---|---|
| Container image | `Dockerfile` (non-root uid 10001, read-only root filesystem compatible) |
| Helm chart | `deployments/helm/clinical-documentation-verification` (StatefulSet, PDB, HPA, NetworkPolicy, ServiceMonitor, projected fabric token) |
| Namespace and secret sync | `deployments/kubernetes` (restricted Pod Security, External Secrets) |
| Cloud infrastructure | `deployments/terraform/aws` (reference: RDS PostgreSQL, KMS, Secrets Manager, S3 Object Lock audit bucket, IRSA with Bedrock invoke on approved models), `azure`, `gcp` |

See the [startup runbook](docs/runbooks/startup.md).

## Documentation

- [Architecture](docs/architecture/overview.md), [agent specification](docs/architecture/agent-specification.md), [clinical safety rules](docs/clinical/safety-rules.md)
- [ADRs](docs/architecture/adr/)
- [Threat model and risk register](docs/security/threat-model.md)
- [Compliance control mapping](docs/compliance/control-mapping.md)
- Runbooks: [startup](docs/runbooks/startup.md), [monitoring](docs/runbooks/monitoring.md), [incident response](docs/runbooks/incident-response.md), [rollback](docs/runbooks/rollback.md), [disaster recovery](docs/runbooks/disaster-recovery.md), [key rotation](docs/runbooks/key-rotation.md), [reference-table change](docs/runbooks/reference-table-change.md)
- [Configuration reference](docs/operations/configuration.md)

## Intended use

Decision support for documentation review by licensed clinicians. The service
does not diagnose, does not place orders and cannot write to the chart on its
own; every finding is shown with its basis so the clinician can review it
independently. Dose ceilings in `clinical/data/formulary.yaml` are screening
limits that pharmacy and therapeutics must review before go-live.

## License

Apache-2.0
