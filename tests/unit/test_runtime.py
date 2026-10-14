"""Authentication, settings validation, composition root, persistence and published contracts."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from agentic_core.domain.classification import DataClassification
from agentic_core.models import Message, ModelRequest, Role
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from clinical_documentation_verification.clinical.formulary import Formulary
from clinical_documentation_verification.config import Settings
from clinical_documentation_verification.contracts import main as contracts_main
from clinical_documentation_verification.domain.errors import ConflictError, UnauthorizedError
from clinical_documentation_verification.domain.models import VerificationStatus
from clinical_documentation_verification.infrastructure.database import (
    SqlVerificationRepository,
    SystemClock,
    create_schema,
    make_engine,
)
from clinical_documentation_verification.integrations.fhir import SmartBackendAuth
from clinical_documentation_verification.runtime.container import build_container
from clinical_documentation_verification.security.jwks import StaticJwks
from clinical_documentation_verification.security.principals import normalize_fhir_user
from tests.conftest import ROOT, make_settings
from tests.fakes import AGENT_ISSUER, CLINICIAN_ISSUER, FakeAgentIssuer, FakeIdp, Upstreams
from tests.unit.test_service import build_verification


def write_pem(path: Path, key: object) -> Path:
    path.write_bytes(
        key.private_bytes(  # type: ignore[attr-defined]
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path


class TestAuthentication:
    async def test_clinician_and_local_agent_tokens(
        self, container: object, clinician_idp: FakeIdp, agent_issuer: FakeAgentIssuer
    ) -> None:
        auth = container.authenticator  # type: ignore[attr-defined]
        token = clinician_idp.mint(
            sub="dr.lee",
            aud="clinical-documentation",
            roles=["physician", 7],
            fhirUser="https://fhir.test/r4/Practitioner/p-9",
        )
        who = await auth.authenticate(f"Bearer {token}")
        assert (who.kind, who.roles, who.fhir_user, who.fhir_user_type) == (
            "user",
            frozenset({"physician"}),
            "Practitioner/p-9",
            "Practitioner",
        )
        assert "token" not in repr(who)
        assert "token" not in who.model_dump()
        odd_roles = clinician_idp.mint(
            sub="dr.lee", aud="clinical-documentation", roles="physician"
        )
        assert (await auth.authenticate(f"Bearer {odd_roles}")).roles == frozenset()

        agent = await auth.authenticate(
            f"Bearer {agent_issuer.mint(act={'sub': 'spiffe://x/agent/a/b'})}"
        )
        assert agent.kind == "agent"
        assert agent.via == "jwks"
        assert agent.delegated
        assert "documentation:verify" in agent.scopes
        no_scope = agent_issuer.mint(scope=None)
        assert (await auth.authenticate(f"Bearer {no_scope}")).scopes == frozenset()

    async def test_rejections(self, container: object, clinician_idp: FakeIdp) -> None:
        auth = container.authenticator  # type: ignore[attr-defined]
        for header in (None, "Basic abc", "Bearer not-a-jwt"):
            with pytest.raises(UnauthorizedError):
                await auth.authenticate(header)
        stranger = FakeIdp("https://elsewhere.test")
        with pytest.raises(UnauthorizedError, match="not trusted"):
            await auth.authenticate(
                f"Bearer {stranger.mint(sub='x', aud='clinical-documentation')}"
            )
        wrong_aud = clinician_idp.mint(sub="x", aud="another-app")
        with pytest.raises(UnauthorizedError, match="InvalidAudience"):
            await auth.authenticate(f"Bearer {wrong_aud}")

    def test_fhir_user_normalization(self) -> None:
        assert normalize_fhir_user("Practitioner/123") == "Practitioner/123"
        assert (
            normalize_fhir_user("https://fhir.test/r4/PractitionerRole/abc-1")
            == "PractitionerRole/abc-1"
        )
        assert normalize_fhir_user("Organization/1") is None
        assert normalize_fhir_user(42) is None


class TestSettings:
    def test_fail_closed(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="cannot be used in production"):
            make_settings(tmp_path, environment="production")
        with pytest.raises(ValueError, match="AUDIT_KEY_FILE"):
            make_settings(tmp_path, dev_ephemeral_audit_key=False)
        with pytest.raises(ValueError, match="fhir_auth=none"):
            make_settings(
                tmp_path,
                environment="production",
                dev_ephemeral_audit_key=False,
                audit_key_file=tmp_path / "k.pem",
            )
        with pytest.raises(ValueError, match="SMART backend-services configuration incomplete"):
            make_settings(tmp_path, fhir_auth="smart-backend")
        with pytest.raises(ValueError, match="set together"):
            make_settings(tmp_path, agent_jwks_url=None)
        with pytest.raises(ValueError, match="default_note_type"):
            make_settings(tmp_path, default_note_type="12345-6")

    def test_reads_environment(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env = {
            "CDV_ENVIRONMENT": "staging",
            "CDV_PUBLIC_URL": "https://cdv.staging.test/",
            "CDV_DATABASE_URL": "sqlite+aiosqlite:///x.db",
            "CDV_FHIR_BASE_URL": "https://fhir.test/r4/",
            "CDV_FHIR_AUTH": "none",
            "CDV_CLINICIAN_ISSUER": CLINICIAN_ISSUER,
            "CDV_CLINICIAN_AUDIENCE": "a",
            "CDV_CLINICIAN_JWKS_URL": "https://login.test/jwks",
            "CDV_DEV_EPHEMERAL_AUDIT_KEY": "true",
        }
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        settings = Settings()
        assert settings.public_url_str == "https://cdv.staging.test"
        assert settings.fhir_base_url_str == "https://fhir.test/r4"


class TestContainer:
    async def test_production_wiring(
        self, tmp_path: Path, clinician_idp: FakeIdp, upstreams: Upstreams
    ) -> None:
        audit_key = write_pem(tmp_path / "audit-2026-10.pem", Ed25519PrivateKey.generate())
        fhir_key = write_pem(tmp_path / "fhir.pem", ec.generate_private_key(ec.SECP384R1()))
        providers = tmp_path / "providers.yaml"
        providers.write_text(
            (ROOT / "configs" / "providers.yaml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        formulary_file = tmp_path / "formulary.yaml"
        formulary_file.write_text(
            'version: "local"\ndrugs:\n  - {name: zz, rxnorm: "1", unit: mg, routes: [PO]}\n',
            encoding="utf-8",
        )
        settings = Settings(
            public_url="https://cdv.test",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'p.db'}",
            fhir_base_url="https://fhir.test/r4",
            fhir_token_url="https://auth.test/token",
            fhir_client_id="cdv-backend",
            fhir_private_key_file=fhir_key,
            fhir_key_id="cdv-2026-10",
            clinician_issuer=CLINICIAN_ISSUER,
            clinician_audience="clinical-documentation",
            clinician_jwks_url="https://login.test/jwks",
            agent_issuer=AGENT_ISSUER,
            agent_jwks_url="https://identity.test/jwks",
            audit_key_file=audit_key,
            audit_log_path=tmp_path / "audit.jsonl",
            providers_file=providers,
            formulary_file=formulary_file,
            policy_file=ROOT / "policies" / "documentation-verification.yaml",
        )
        env = {
            "AWS_REGION": "us-east-1",
            "CDV_BEDROCK_PRIMARY_MODEL_ID": "us.amazon.nova-premier-v1:0",
            "CDV_BEDROCK_PRIMARY_INPUT_USD_PER_MTOK": "3",
            "CDV_BEDROCK_PRIMARY_OUTPUT_USD_PER_MTOK": "15",
            "CDV_BEDROCK_FALLBACK_INPUT_USD_PER_MTOK": "0.8",
            "CDV_BEDROCK_FALLBACK_OUTPUT_USD_PER_MTOK": "3.2",
        }
        with pytest.MonkeyPatch.context() as mp:
            for key, value in env.items():
                mp.setenv(key, value)
            container = await build_container(
                settings,
                clinician_resolver=StaticJwks(clinician_idp.jwks()),
                http=upstreams.client(),
            )
        try:
            assert container.audit._key_id == "audit-2026-10"
            assert container.router is not None
            request = ModelRequest(
                messages=(Message(role=Role.USER, content="x"),),
                classification=DataClassification.RESTRICTED,
                task="extract",
            )
            assert [p.profile.name for p in container.router.plan(request)] == [
                "bedrock-primary",
                "bedrock-fallback",
            ]
            assert container.formulary.version == "local"
            assert isinstance(container.service._chart._client._auth, SmartBackendAuth)  # type: ignore[attr-defined]
        finally:
            await container.aclose()

    async def test_rejects_non_ed25519_audit_key(self, tmp_path: Path) -> None:
        key = write_pem(tmp_path / "audit.pem", ec.generate_private_key(ec.SECP256R1()))
        settings = make_settings(tmp_path, dev_ephemeral_audit_key=False, audit_key_file=key)
        with pytest.raises(ValueError, match="Ed25519"):
            await build_container(settings)


class TestPersistence:
    async def test_repository(self, tmp_path: Path, formulary: Formulary) -> None:
        engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
        await create_schema(engine)
        await create_schema(engine)  # idempotent
        repo = SqlVerificationRepository(engine)
        v = build_verification(formulary).model_copy(update={"idempotency_key": "k-1"})
        await repo.add(v)
        assert await repo.get(v.id) == v
        assert await repo.find_by_idempotency_key("k-1") == v
        assert await repo.find_by_idempotency_key("missing") is None
        assert await repo.get(str(uuid.uuid4())) is None
        with pytest.raises(ConflictError):
            await repo.add(v.model_copy(update={"id": str(uuid.uuid4())}))
        approved = v.transition(VerificationStatus.APPROVED, at=SystemClock().now())
        await repo.update(approved, expected_version=v.version)
        with pytest.raises(ConflictError, match="concurrently"):
            await repo.update(approved, expected_version=v.version)
        assert [x.id for x in await repo.list(status=VerificationStatus.APPROVED)] == [v.id]
        assert await repo.list(patient_id="other") == []
        await engine.dispose()


def test_committed_contracts_are_current() -> None:
    assert contracts_main(["--check"], root=ROOT) == 0


def test_contract_export_round_trip(tmp_path: Path) -> None:
    assert contracts_main(["--check"], root=tmp_path) == 1
    assert contracts_main([], root=tmp_path) == 0
    assert contracts_main(["--check"], root=tmp_path) == 0
    assert (tmp_path / "schemas" / "submit-verification.request.schema.json").is_file()
    assert (tmp_path / "docs" / "api" / "openapi.json").is_file()


class TestRemoteJwks:
    async def test_cache_refresh_and_outage(self, clinician_idp: FakeIdp) -> None:
        import httpx

        from clinical_documentation_verification.security.jwks import RemoteJwks

        calls = {"n": 0, "down": False}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["down"]:
                return httpx.Response(503)
            return httpx.Response(200, json=clinician_idp.jwks())

        now = {"t": 1000.0}
        jwks = RemoteJwks(
            "https://login.test/jwks",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            ttl_s=300,
            min_refresh_interval_s=30,
            clock=lambda: now["t"],
        )
        assert await jwks.key_for("idp-1") is not None
        assert await jwks.key_for("idp-1") is not None
        assert calls["n"] == 1  # cached
        with pytest.raises(UnauthorizedError, match="unknown signing key"):
            await jwks.key_for("rotated")  # refresh rate-limited: no second fetch yet
        assert calls["n"] == 1
        now["t"] += 31
        with pytest.raises(UnauthorizedError, match="unknown signing key"):
            await jwks.key_for("rotated")
        assert calls["n"] == 2
        calls["down"] = True
        now["t"] += 400
        assert await jwks.key_for("idp-1") is not None  # stale key tolerated during an outage
        with pytest.raises(UnauthorizedError, match="cannot fetch"):
            await jwks.key_for("new-kid")


def test_routing_issuer_reads_only_well_formed_jwts() -> None:
    import base64
    import json

    from clinical_documentation_verification.security.principals import routing_issuer

    def b64(value: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    assert routing_issuer(f"h.{b64({'iss': 'https://a.test'})}.s") == "https://a.test"
    assert routing_issuer("opaque-token") is None
    assert routing_issuer("a.!!!.c") is None
    assert routing_issuer(f"h.{b64([1, 2])}.s") is None
    assert routing_issuer(f"h.{b64({'iss': 5})}.s") is None
