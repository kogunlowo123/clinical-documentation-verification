"""Runtime settings from CDV_* environment variables. Incomplete configuration fails closed."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from clinical_documentation_verification.integrations.fhir_documents import NOTE_TYPES


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CDV_", env_nested_delimiter="__")

    environment: str = "production"
    public_url: HttpUrl = Field(description="Public base URL; advertised in the A2A agent card")
    database_url: str = Field(description="SQLAlchemy async URL, e.g. postgresql+asyncpg://...")

    # Model access. Without a providers file extraction is deterministic only.
    providers_file: Path | None = None
    extraction_max_attempts: int = Field(default=3, ge=1, le=5)
    extraction_max_tokens: int = Field(default=4096, ge=256, le=32_000)
    formulary_file: Path | None = None
    policy_file: Path = Path("policies/documentation-verification.yaml")
    default_note_type: str = "11506-3"

    # FHIR server (chart reads and the approved write).
    fhir_base_url: HttpUrl
    fhir_auth: Literal["smart-backend", "none"] = "smart-backend"
    fhir_token_url: str | None = None
    fhir_client_id: str | None = None
    fhir_private_key_file: Path | None = None
    fhir_key_id: str | None = None
    fhir_signing_alg: Literal["RS384", "ES384"] = "ES384"
    fhir_scope: str = (
        "system/Patient.read system/Condition.read system/MedicationRequest.read "
        "system/AllergyIntolerance.read system/DocumentReference.write system/Provenance.write"
    )

    # Clinician authentication (enterprise OIDC).
    clinician_issuer: str
    clinician_audience: str
    clinician_jwks_url: str
    clinician_roles_claim: str = "roles"
    clinician_fhir_user_claim: str = "fhirUser"

    # Agent identity: fabric introspection/authorization, or local JWKS verification.
    fabric_url: str | None = None
    agent_id: str = Field(
        default="spiffe://agents.local/agent/clinical/documentation-verification",
        description="SPIFFE ID of this agent as registered in the identity fabric",
    )
    workload_token_file: Path | None = None
    agent_issuer: str | None = None
    agent_audience: str = "agent-platform"
    agent_jwks_url: str | None = None

    # Audit evidence.
    audit_key_file: Path | None = None
    audit_log_path: Path = Path("/var/lib/clinical-documentation-verification/audit/audit.jsonl")
    dev_ephemeral_audit_key: bool = Field(
        default=False, description="In-memory audit key. Refused when environment=production."
    )

    log_level: str = "INFO"
    otlp_endpoint: str | None = None

    @model_validator(mode="after")
    def _fail_closed(self) -> Settings:
        production = self.environment == "production"
        if self.dev_ephemeral_audit_key and production:
            raise ValueError("dev_ephemeral_audit_key cannot be used in production")
        if not self.dev_ephemeral_audit_key and self.audit_key_file is None:
            raise ValueError("CDV_AUDIT_KEY_FILE is required (or a non-production ephemeral key)")
        if self.fhir_auth == "none" and production:
            raise ValueError("fhir_auth=none cannot be used in production")
        if self.fhir_auth == "smart-backend":
            missing = [
                name
                for name, value in (
                    ("CDV_FHIR_TOKEN_URL", self.fhir_token_url),
                    ("CDV_FHIR_CLIENT_ID", self.fhir_client_id),
                    ("CDV_FHIR_PRIVATE_KEY_FILE", self.fhir_private_key_file),
                    ("CDV_FHIR_KEY_ID", self.fhir_key_id),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"SMART backend-services configuration incomplete: {missing}")
        if (self.agent_issuer is None) != (self.agent_jwks_url is None):
            raise ValueError("CDV_AGENT_ISSUER and CDV_AGENT_JWKS_URL must be set together")
        if self.default_note_type not in NOTE_TYPES:
            raise ValueError(f"default_note_type must be one of {sorted(NOTE_TYPES)}")
        return self

    @property
    def public_url_str(self) -> str:
        return str(self.public_url).rstrip("/")

    @property
    def fhir_base_url_str(self) -> str:
        return str(self.fhir_base_url).rstrip("/")
