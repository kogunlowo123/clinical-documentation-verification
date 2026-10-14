# Configuration reference

All settings are `CDV_*` environment variables (see
`src/clinical_documentation_verification/config.py`). The service refuses to
start with incomplete or unsafe configuration.

| Variable | Required | Default | Description |
|---|---|---|---|
| `CDV_ENVIRONMENT` | no | `production` | `production` refuses an ephemeral audit key and unauthenticated FHIR |
| `CDV_PUBLIC_URL` | yes | | Public base URL (A2A agent card) |
| `CDV_DATABASE_URL` | yes | | SQLAlchemy async URL |
| `CDV_FHIR_BASE_URL` | yes | | FHIR R4 base URL |
| `CDV_FHIR_AUTH` | no | `smart-backend` | `none` only outside production |
| `CDV_FHIR_TOKEN_URL`, `CDV_FHIR_CLIENT_ID`, `CDV_FHIR_PRIVATE_KEY_FILE`, `CDV_FHIR_KEY_ID` | yes* | | SMART backend-services client (*with `smart-backend`) |
| `CDV_FHIR_SIGNING_ALG` | no | `ES384` | `ES384` or `RS384` |
| `CDV_FHIR_SCOPE` | no | read scopes + DocumentReference/Provenance write | Requested system scopes |
| `CDV_CLINICIAN_ISSUER`, `CDV_CLINICIAN_AUDIENCE`, `CDV_CLINICIAN_JWKS_URL` | yes | | Enterprise OIDC for clinicians |
| `CDV_CLINICIAN_ROLES_CLAIM` | no | `roles` | Claim with clinician roles |
| `CDV_CLINICIAN_FHIR_USER_CLAIM` | no | `fhirUser` | SMART claim naming the Practitioner |
| `CDV_FABRIC_URL` | no | | Agent identity fabric; enables introspection and fabric authorization |
| `CDV_AGENT_ID` | no | `spiffe://agents.local/agent/clinical/documentation-verification` | This service's SPIFFE ID in the fabric |
| `CDV_WORKLOAD_TOKEN_FILE` | no | | Projected token exchanged for the service's own credential |
| `CDV_AGENT_ISSUER`, `CDV_AGENT_JWKS_URL` | no (together) | | Local verification of fabric credentials when no fabric URL is set |
| `CDV_AGENT_AUDIENCE` | no | `agent-platform` | Audience of agent credentials |
| `CDV_PROVIDERS_FILE` | no | | Model providers (`configs/providers.yaml`); unset = rule-based extraction only |
| `CDV_EXTRACTION_MAX_ATTEMPTS` | no | `3` | Schema-corrective attempts (1–5) |
| `CDV_EXTRACTION_MAX_TOKENS` | no | `4096` | Model output cap |
| `CDV_FORMULARY_FILE` | no | bundled table | Medication reference table override |
| `CDV_POLICY_FILE` | no | `policies/documentation-verification.yaml` | Domain policy |
| `CDV_DEFAULT_NOTE_TYPE` | no | `11506-3` | LOINC note type |
| `CDV_AUDIT_KEY_FILE` | yes* | | Ed25519 PEM (*unless `CDV_DEV_EPHEMERAL_AUDIT_KEY`) |
| `CDV_AUDIT_LOG_PATH` | no | `/var/lib/clinical-documentation-verification/audit/audit.jsonl` | Audit chain file |
| `CDV_DEV_EPHEMERAL_AUDIT_KEY` | no | `false` | Non-production only |
| `CDV_LOG_LEVEL` | no | `INFO` | |
| `CDV_OTLP_ENDPOINT` | no | | OTLP gRPC endpoint for traces |
| `CDV_HOST` / `CDV_PORT` | no | `127.0.0.1` / `8080` | Container image sets host `0.0.0.0` |
| `CDV_FORWARDED_ALLOW_IPS` | no | `127.0.0.1` | Proxies trusted for `X-Forwarded-*` |

## Provider configuration variables (`configs/providers.yaml`)

| Variable | Description |
|---|---|
| `AWS_REGION` | Bedrock region |
| `CDV_BEDROCK_PRIMARY_MODEL_ID` | Primary Bedrock model or inference profile ID |
| `CDV_BEDROCK_PRIMARY_INPUT_USD_PER_MTOK`, `CDV_BEDROCK_PRIMARY_OUTPUT_USD_PER_MTOK` | Contracted prices |
| `CDV_BEDROCK_FALLBACK_INPUT_USD_PER_MTOK`, `CDV_BEDROCK_FALLBACK_OUTPUT_USD_PER_MTOK` | Contracted prices for Amazon Nova Pro |

AWS credentials come from the default chain (IRSA / EKS Pod Identity).
