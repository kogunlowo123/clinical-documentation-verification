"""Shared fixtures: settings, fake upstream systems, a container wired to SQLite, and an app."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from agentic_core.domain.classification import DataClassification
from agentic_core.models import ProviderProfile
from agentic_core.models.providers import OpenAICompatibleProvider
from fastapi.testclient import TestClient

from clinical_documentation_verification.api.app import create_app
from clinical_documentation_verification.application.service import VerificationService
from clinical_documentation_verification.clinical.formulary import Formulary
from clinical_documentation_verification.config import Settings
from clinical_documentation_verification.runtime.container import Container, build_container
from clinical_documentation_verification.security.jwks import StaticJwks
from tests.fakes import (
    AGENT_ISSUER,
    CLINICIAN_ISSUER,
    FHIR_BASE,
    LLM_URL,
    FakeAgentIssuer,
    FakeClock,
    FakeIdp,
    FakeModel,
    Upstreams,
)

ROOT = Path(__file__).resolve().parents[1]
PRACTITIONER = "Practitioner/prac-77"

# Synthetic encounters. No real patient is described.
ENCOUNTER = """[00:00:05] Dr. Rivera: Good morning. What brings you in today?
[00:00:09] Patient: My blood pressure has been high at home and I've been getting headaches.
[00:00:20] Dr. Rivera: Are you taking your medications?
[00:00:24] Patient: I take lisinopril 10 mg once a day and aspirin 81 mg daily.
I stopped taking the atorvastatin because of muscle aches.
[00:00:40] Patient: I'm allergic to penicillin, it gives me hives.
[00:01:02] Dr. Rivera: On exam your blood pressure is 152/94. Lungs are clear.
[00:01:30] Dr. Rivera: For your hypertension, let's increase lisinopril to 20 mg by mouth daily.
I'm also going to start amoxicillin 500 mg by mouth three times a day for the sinus infection.
[00:02:05] Dr. Rivera: Let's check an A1c and a basic metabolic panel.
I'll refer you to cardiology.
"""

CLEAN_ENCOUNTER = """Dr. Okafor: How have you been since the last visit?
Patient: Pretty good. I take lisinopril 10 mg daily, aspirin 81 mg daily
and atorvastatin 40 mg at bedtime.
Patient: I'm allergic to penicillin.
Dr. Okafor: Your blood pressure is 126/78 today, so your hypertension is well controlled.
Dr. Okafor: Continue lisinopril 10 mg by mouth daily
and continue atorvastatin 40 mg by mouth at bedtime.
Dr. Okafor: Let's check a lipid panel before the next visit.
"""


def model_answer(**overrides: Any) -> str:
    import json

    body: dict[str, Any] = {
        "problems": [{"name": "hypertension", "status": "active", "evidence": [3]}],
        "medications": [
            {
                "name": "lisinopril",
                "sig_text": "10 mg daily",
                "action": "continue",
                "evidence": [1, 3],
            },
            {
                "name": "atorvastatin",
                "sig_text": "40 mg at bedtime",
                "action": "continue",
                "evidence": [1, 3],
            },
            {"name": "aspirin", "sig_text": "81 mg daily", "action": "reported", "evidence": [1]},
        ],
        "orders": [{"kind": "lab", "description": "lipid panel", "evidence": [4]}],
        "allergies": [{"substance": "penicillin", "evidence": [2]}],
        "no_known_allergies": False,
    }
    body.update(overrides)
    return json.dumps(body)


@pytest.fixture
def clinician_idp() -> FakeIdp:
    return FakeIdp(CLINICIAN_ISSUER, kid="idp-1")


@pytest.fixture
def agent_issuer() -> FakeAgentIssuer:
    return FakeAgentIssuer()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def upstreams() -> Upstreams:
    return Upstreams()


@pytest.fixture
def formulary() -> Formulary:
    return Formulary.bundled()


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    db = os.environ.get("CDV_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'cdv.db'}")
    values: dict[str, Any] = {
        "environment": "test",
        "public_url": "https://cdv.test",
        "database_url": db,
        "fhir_base_url": FHIR_BASE,
        "fhir_auth": "none",
        "clinician_issuer": CLINICIAN_ISSUER,
        "clinician_audience": "clinical-documentation",
        "clinician_jwks_url": "https://login.test/jwks",
        "agent_issuer": AGENT_ISSUER,
        "agent_jwks_url": "https://identity.test/.well-known/jwks.json",
        "dev_ephemeral_audit_key": True,
        "audit_log_path": tmp_path / "audit.jsonl",
        "policy_file": ROOT / "policies" / "documentation-verification.yaml",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


def model_provider(model: FakeModel) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        ProviderProfile(
            name="test-llm",
            model="clinical-extractor",
            max_classification=DataClassification.RESTRICTED,
            tasks=frozenset({"extract"}),
        ),
        base_url=LLM_URL,
        auth="none",
        transport=httpx.MockTransport(model.handle),
    )


async def make_container(
    settings: Settings,
    upstreams: Upstreams,
    clinician_idp: FakeIdp,
    agent_issuer: FakeAgentIssuer,
    clock: FakeClock,
    *,
    model: FakeModel | None = None,
) -> Container:
    return await build_container(
        settings,
        clinician_resolver=StaticJwks(clinician_idp.jwks()),
        agent_resolver=StaticJwks(agent_issuer.jwks()),
        clock=clock,
        http=upstreams.client(),
        providers=[model_provider(model)] if model is not None else None,
    )


@pytest.fixture
async def container(
    settings: Settings,
    upstreams: Upstreams,
    clinician_idp: FakeIdp,
    agent_issuer: FakeAgentIssuer,
    clock: FakeClock,
) -> AsyncIterator[Container]:
    built = await make_container(settings, upstreams, clinician_idp, agent_issuer, clock)
    yield built
    await built.aclose()


@pytest.fixture
def service(container: Container) -> VerificationService:
    return container.service


def clinician_headers(
    idp: FakeIdp,
    *roles: str,
    fhir_user: str | None = PRACTITIONER,
    sub: str = "dr.rivera@hospital.test",
) -> dict[str, str]:
    extra: dict[str, Any] = {"roles": list(roles or ("physician",))}
    if fhir_user is not None:
        extra["fhirUser"] = f"{FHIR_BASE}/{fhir_user}"
    token = idp.mint(sub=sub, aud="clinical-documentation", **extra)
    return {"Authorization": f"Bearer {token}"}


def agent_headers(issuer: FakeAgentIssuer, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {issuer.mint(**kwargs)}"}


def submission(text: str = ENCOUNTER, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "patient_id": "pat-1001",
        "encounter_id": "enc-2001",
        "transcript": {"text": text},
    }
    body.update(overrides)
    return body


@pytest.fixture
def client(
    settings: Settings,
    upstreams: Upstreams,
    clinician_idp: FakeIdp,
    agent_issuer: FakeAgentIssuer,
    clock: FakeClock,
) -> Iterator[TestClient]:
    """App whose container is built inside the app's own event loop by its lifespan."""

    async def factory() -> Container:
        return await make_container(settings, upstreams, clinician_idp, agent_issuer, clock)

    with TestClient(create_app(factory)) as test_client:
        yield test_client
