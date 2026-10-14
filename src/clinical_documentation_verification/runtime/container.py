"""Composition root: builds every dependency once per process from Settings."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import httpx
from agentic_core.audit import AuditLog, JsonlAuditSink
from agentic_core.models.factory import ProvidersConfig, build_providers
from agentic_core.models.router import ModelRouter
from agentic_core.models.types import ModelProvider
from agentic_core.observability import ModelTelemetryObserver
from agentic_core.policy import PolicyEngine, PolicySet
from agentic_core.security import ContentGuard
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import AsyncEngine

from clinical_documentation_verification.application.service import VerificationService
from clinical_documentation_verification.clinical.formulary import Formulary
from clinical_documentation_verification.config import Settings
from clinical_documentation_verification.domain.ports import Clock
from clinical_documentation_verification.infrastructure.database import (
    SqlVerificationRepository,
    SystemClock,
    create_schema,
    make_engine,
)
from clinical_documentation_verification.integrations.fhir import (
    FhirAuth,
    FhirChartSource,
    FhirClient,
    FhirRecordWriter,
    NoAuth,
    SmartBackendAuth,
)
from clinical_documentation_verification.integrations.identity import (
    AgentCredentialSource,
    IdentityFabricClient,
)
from clinical_documentation_verification.observability.metrics import VerificationMetrics
from clinical_documentation_verification.orchestration.extraction_agent import ExtractionAgent
from clinical_documentation_verification.security.authorization import AccessController
from clinical_documentation_verification.security.jwks import KeyResolver, RemoteJwks
from clinical_documentation_verification.security.principals import (
    AgentJwtVerifier,
    Authenticator,
    ClinicianVerifier,
)


@dataclass
class Container:
    settings: Settings
    service: VerificationService
    authenticator: Authenticator
    access: AccessController
    audit: AuditLog
    metrics: VerificationMetrics
    formulary: Formulary
    engine: AsyncEngine
    http: httpx.AsyncClient
    router: ModelRouter | None = None

    async def aclose(self) -> None:
        if self.router is not None:
            await self.router.aclose()
        await self.http.aclose()
        await self.engine.dispose()


def _audit_signer(settings: Settings) -> tuple[Ed25519PrivateKey, str]:
    if settings.audit_key_file is None:
        return Ed25519PrivateKey.generate(), "audit-ephemeral"
    key = serialization.load_pem_private_key(settings.audit_key_file.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("audit key must be Ed25519")
    return key, settings.audit_key_file.stem


def _fhir_auth(settings: Settings, http: httpx.AsyncClient, clock: Clock) -> FhirAuth:
    if settings.fhir_auth == "none":
        return NoAuth()
    if (
        settings.fhir_private_key_file is None
        or settings.fhir_token_url is None
        or settings.fhir_client_id is None
        or settings.fhir_key_id is None
    ):  # pragma: no cover - rejected by Settings validation
        raise ValueError("SMART backend-services configuration incomplete")
    key = serialization.load_pem_private_key(
        settings.fhir_private_key_file.read_bytes(), password=None
    )
    return SmartBackendAuth(
        token_url=settings.fhir_token_url,
        client_id=settings.fhir_client_id,
        private_key=key,
        key_id=settings.fhir_key_id,
        algorithm=settings.fhir_signing_alg,
        scope=settings.fhir_scope,
        http=http,
        clock=clock.now,
    )


async def build_container(
    settings: Settings,
    *,
    clinician_resolver: KeyResolver | None = None,
    agent_resolver: KeyResolver | None = None,
    clock: Clock | None = None,
    http: httpx.AsyncClient | None = None,
    providers: Sequence[ModelProvider] | None = None,
    model_transport: httpx.AsyncBaseTransport | None = None,
) -> Container:
    http = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
    clock = clock or SystemClock()
    formulary = (
        Formulary.from_file(settings.formulary_file)
        if settings.formulary_file
        else Formulary.bundled()
    )

    engine = make_engine(settings.database_url)
    await create_schema(engine)
    signer, key_id = _audit_signer(settings)
    audit = AuditLog(JsonlAuditSink(settings.audit_log_path), signer, key_id=key_id)

    fabric = IdentityFabricClient(settings.fabric_url, http=http) if settings.fabric_url else None
    access = AccessController(
        PolicyEngine(PolicySet.from_file(settings.policy_file)), audit, fabric=fabric
    )

    fhir = FhirClient(settings.fhir_base_url_str, http=http, auth=_fhir_auth(settings, http, clock))

    if providers is None and settings.providers_file is not None:
        providers = build_providers(
            ProvidersConfig.from_file(settings.providers_file), transport=model_transport
        )
    router = ModelRouter(providers, observers=[ModelTelemetryObserver()]) if providers else None
    agent = (
        ExtractionAgent(
            router,
            ContentGuard(),
            formulary,
            max_attempts=settings.extraction_max_attempts,
            max_tokens=settings.extraction_max_tokens,
        )
        if router
        else None
    )

    credentials = (
        AgentCredentialSource(
            fabric,
            agent_id=settings.agent_id,
            workload_token_file=settings.workload_token_file,
            clock=clock.now,
        )
        if fabric is not None and settings.workload_token_file is not None
        else None
    )
    agents = None
    if settings.agent_issuer is not None and settings.agent_jwks_url is not None:
        agents = AgentJwtVerifier(
            resolver=agent_resolver or RemoteJwks(settings.agent_jwks_url, client=http),
            issuer=settings.agent_issuer,
            audience=settings.agent_audience,
        )
    authenticator = Authenticator(
        ClinicianVerifier(
            resolver=clinician_resolver or RemoteJwks(settings.clinician_jwks_url, client=http),
            issuer=settings.clinician_issuer,
            audience=settings.clinician_audience,
            roles_claim=settings.clinician_roles_claim,
            fhir_user_claim=settings.clinician_fhir_user_claim,
        ),
        fabric=fabric,
        agents=agents,
    )
    metrics = VerificationMetrics()
    service = VerificationService(
        repository=SqlVerificationRepository(engine),
        chart=FhirChartSource(fhir, formulary),
        writer=FhirRecordWriter(fhir, clock.now),
        access=access,
        audit=audit,
        formulary=formulary,
        metrics=metrics,
        clock=clock,
        agent_id=settings.agent_id,
        default_note_type=settings.default_note_type,
        extraction_agent=agent,
        credentials=credentials,
    )
    return Container(
        settings=settings,
        service=service,
        authenticator=authenticator,
        access=access,
        audit=audit,
        metrics=metrics,
        formulary=formulary,
        engine=engine,
        http=http,
        router=router,
    )
