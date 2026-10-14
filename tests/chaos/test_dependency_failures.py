"""Fault injection: the service fails closed, keeps no partial state and recovers cleanly."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from clinical_documentation_verification.application.service import VerificationService
from clinical_documentation_verification.infrastructure.database import make_engine
from tests.conftest import CLEAN_ENCOUNTER, clinician_headers, submission
from tests.fakes import FakeIdp, Upstreams

pytestmark = pytest.mark.chaos
ATTEST = {"statement": "I reviewed and verified this documentation."}


def test_fhir_outage_during_chart_review_persists_nothing(
    client: TestClient, clinician_idp: FakeIdp, upstreams: Upstreams
) -> None:
    upstreams.fhir.fail_gets = 10
    headers = clinician_headers(clinician_idp)
    response = client.post("/v1/verifications", json=submission(CLEAN_ENCOUNTER), headers=headers)
    assert response.status_code == 503
    assert response.json()["error"] == "upstream_unavailable"
    assert client.get("/v1/verifications", headers=headers).json()["items"] == []


def test_single_transient_fhir_error_is_absorbed(
    client: TestClient, clinician_idp: FakeIdp, upstreams: Upstreams
) -> None:
    upstreams.fhir.fail_gets = 1
    response = client.post(
        "/v1/verifications",
        json=submission(CLEAN_ENCOUNTER),
        headers=clinician_headers(clinician_idp),
    )
    assert response.status_code == 201


def test_failed_chart_write_is_retried_idempotently(
    client: TestClient, clinician_idp: FakeIdp, upstreams: Upstreams
) -> None:
    headers = clinician_headers(clinician_idp, "physician")
    created = client.post(
        "/v1/verifications", json=submission(CLEAN_ENCOUNTER), headers=headers
    ).json()
    upstreams.fhir.fail_transactions = 1
    failed = client.post(f"/v1/verifications/{created['id']}/approve", json=ATTEST, headers=headers)
    assert failed.status_code == 503
    state = client.get(f"/v1/verifications/{created['id']}", headers=headers).json()
    assert state["status"] == "approved"
    assert state["write"] is None
    retried = client.post(
        f"/v1/verifications/{created['id']}/approve", json=ATTEST, headers=headers
    )
    assert retried.json()["status"] == "written"
    assert len(upstreams.fhir.written("DocumentReference")) == 1
    metrics = client.get("/metrics").text
    assert 'cdv_fhir_writes_total{outcome="failed"} 1.0' in metrics
    assert 'cdv_fhir_writes_total{outcome="written"} 1.0' in metrics


def test_fhir_rejection_of_write_is_reported(
    client: TestClient, clinician_idp: FakeIdp, upstreams: Upstreams
) -> None:
    headers = clinician_headers(clinician_idp, "physician")
    created = client.post(
        "/v1/verifications", json=submission(CLEAN_ENCOUNTER), headers=headers
    ).json()
    upstreams.fhir.fail_transactions = 1
    upstreams.fhir.transaction_status = 422
    rejected = client.post(
        f"/v1/verifications/{created['id']}/approve", json=ATTEST, headers=headers
    )
    assert rejected.status_code == 502
    assert "write failed" in rejected.json()["error_description"]


def test_database_outage_flips_readiness(client: TestClient) -> None:
    container = client.app.state.container  # type: ignore[attr-defined]
    assert client.get("/readyz").status_code == 200
    healthy = container.engine
    container.engine = make_engine("sqlite+aiosqlite:////nonexistent/dir/db.sqlite")
    try:
        assert client.get("/readyz").status_code == 503
        assert client.get("/healthz").status_code == 200  # liveness unaffected
    finally:
        container.engine = healthy


async def test_audit_failure_blocks_the_workflow(
    service: VerificationService, upstreams: Upstreams
) -> None:
    """If evidence cannot be written, the action must not complete silently."""
    from tests.unit.test_service import clinician, submit_cmd

    async def broken_append(_: object) -> None:
        raise OSError("disk full")

    service._audit._sink.append = broken_append  # type: ignore[method-assign]
    with pytest.raises(OSError, match="disk full"):
        await service.submit(submit_cmd(), clinician())
    assert upstreams.fhir.transactions == []


def test_fabric_outage_rejects_agent_calls(
    tmp_path: object,
    upstreams: Upstreams,
    clinician_idp: FakeIdp,
    agent_issuer: object,
    clock: object,
) -> None:
    from pathlib import Path

    from clinical_documentation_verification.api.app import create_app
    from tests.conftest import make_container, make_settings

    settings = make_settings(
        Path(str(tmp_path)),
        fabric_url="https://fabric.test",
        agent_issuer=None,
        agent_jwks_url=None,
    )

    async def factory():  # type: ignore[no-untyped-def]
        return await make_container(settings, upstreams, clinician_idp, agent_issuer, clock)  # type: ignore[arg-type]

    token = upstreams.fabric.credential(
        "spiffe://agents.test/agent/clinical/scribe", "documentation:read"
    )
    with TestClient(create_app(factory)) as client:
        assert (
            client.get(
                "/v1/verifications", headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 200
        )
        upstreams.fabric.down = True
        response = client.get("/v1/verifications", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 503
        # Clinician access does not depend on the fabric.
        assert (
            client.get("/v1/verifications", headers=clinician_headers(clinician_idp)).status_code
            == 200
        )
