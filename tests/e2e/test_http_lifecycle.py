"""Full workflow over HTTP, as an ambient-scribe agent and a signing clinician would use it."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    CLEAN_ENCOUNTER,
    ENCOUNTER,
    PRACTITIONER,
    agent_headers,
    clinician_headers,
    submission,
)
from tests.fakes import FakeAgentIssuer, FakeIdp, Upstreams

pytestmark = pytest.mark.e2e

ATTEST = "I have reviewed this note against the encounter and verified it."


def test_agent_submits_clinician_signs_and_chart_is_written(
    client: TestClient, agent_issuer: FakeAgentIssuer, clinician_idp: FakeIdp, upstreams: Upstreams
) -> None:
    submitted = client.post(
        "/v1/verifications",
        json=submission(CLEAN_ENCOUNTER),
        headers={**agent_headers(agent_issuer), "Idempotency-Key": "scribe-msg-0001"},
    )
    assert submitted.status_code == 201, submitted.text
    body = submitted.json()
    assert body["status"] == "pending_approval"
    assert body["write_policy"]["decision"] == "require_approval"
    assert body["consistency"]["band"] == "consistent", body["consistency"]
    assert upstreams.fhir.transactions == []  # nothing written before sign-off

    again = client.post(
        "/v1/verifications",
        json=submission(CLEAN_ENCOUNTER),
        headers={**agent_headers(agent_issuer), "Idempotency-Key": "scribe-msg-0001"},
    )
    assert again.json()["id"] == body["id"]

    fetched = client.get(
        f"/v1/verifications/{body['id']}", headers=clinician_headers(clinician_idp)
    )
    assert fetched.status_code == 200
    assert "PLAN AND ORDERS" in fetched.json()["draft_note"]

    approved = client.post(
        f"/v1/verifications/{body['id']}/approve",
        json={"statement": ATTEST},
        headers=clinician_headers(clinician_idp, "physician"),
    )
    assert approved.status_code == 200, approved.text
    result = approved.json()
    assert result["status"] == "written"
    assert result["write"]["document_reference"].startswith("DocumentReference/")
    assert result["clinician_decision"]["practitioner"] == PRACTITIONER

    [doc] = upstreams.fhir.written("DocumentReference")
    [prov] = upstreams.fhir.written("Provenance")
    note = base64.b64decode(doc["content"][0]["attachment"]["data"]).decode()
    assert ATTEST in note
    assert doc["docStatus"] == "final"
    assert doc["authenticator"] == {"reference": PRACTITIONER}
    assert {a["type"]["coding"][0]["code"] for a in prov["agent"]} == {
        "author",
        "verifier",
        "assembler",
    }
    assert any(e["what"].get("reference", "").startswith("Condition/") for e in prov["entity"])

    # Approving again is idempotent and does not write twice.
    repeat = client.post(
        f"/v1/verifications/{body['id']}/approve",
        json={"statement": ATTEST},
        headers=clinician_headers(clinician_idp, "physician"),
    )
    assert repeat.json()["status"] == "written"
    assert len(upstreams.fhir.transactions) == 1

    listing = client.get(
        "/v1/verifications",
        params={"patient_id": "pat-1001"},
        headers=clinician_headers(clinician_idp),
    )
    assert [i["id"] for i in listing.json()["items"]] == [body["id"]]
    assert listing.json()["items"][0]["document_reference"] == result["write"]["document_reference"]


def test_critical_finding_needs_override_then_signs(
    client: TestClient, clinician_idp: FakeIdp, upstreams: Upstreams
) -> None:
    headers = clinician_headers(clinician_idp, "physician")
    created = client.post("/v1/verifications", json=submission(ENCOUNTER), headers=headers).json()
    assert created["consistency"]["band"] == "inconsistent"
    rules = {f["rule"] for f in created["findings"]}
    assert "allergy-contraindication" in rules
    kinds = {(d["item"], d["kind"]) for d in created["chart_diff"]}
    assert ("atorvastatin", "discontinued") in kinds
    assert ("lisinopril", "changed") in kinds

    denied = client.post(
        f"/v1/verifications/{created['id']}/approve", json={"statement": ATTEST}, headers=headers
    )
    assert denied.status_code == 403
    assert "critical" in denied.json()["error_description"]

    signed = client.post(
        f"/v1/verifications/{created['id']}/approve",
        json={
            "statement": ATTEST,
            "override_reason": "Amoxicillin order cancelled; doxycycline prescribed separately.",
            "amended_note": "Progress note amended by clinician: amoxicillin order withdrawn.",
        },
        headers=headers,
    )
    assert signed.status_code == 200, signed.text
    note = base64.b64decode(
        upstreams.fhir.written("DocumentReference")[0]["content"][0]["attachment"]["data"]
    ).decode()
    assert note.startswith("Progress note amended by clinician")


def test_reject_closes_the_verification(client: TestClient, clinician_idp: FakeIdp) -> None:
    headers = clinician_headers(clinician_idp, "nurse-practitioner")
    created = client.post(
        "/v1/verifications", json=submission(CLEAN_ENCOUNTER), headers=headers
    ).json()
    rejected = client.post(
        f"/v1/verifications/{created['id']}/reject",
        json={"reason": "Transcript belongs to a different encounter."},
        headers=headers,
    )
    assert rejected.json()["status"] == "rejected"
    again = client.post(
        f"/v1/verifications/{created['id']}/approve", json={"statement": ATTEST}, headers=headers
    )
    assert again.status_code == 409


def test_order_tools_over_rest(client: TestClient, clinician_idp: FakeIdp) -> None:
    headers = clinician_headers(clinician_idp, "registered-nurse")
    checked = client.post(
        "/v1/orders/validate",
        json={
            "patient_id": "pat-1001",
            "orders": [{"text": "ibuprofen 800 mg PO q4h"}, {"text": "cephalexin 500 mg PO QID"}],
        },
        headers=headers,
    )
    assert checked.status_code == 200, checked.text
    rules = {f["rule"] for f in checked.json()["findings"]}
    assert {"dose-range-daily", "allergy-cross-reactivity"} <= rules
    assert checked.json()["safe_to_sign"] is False
    parsed = client.post(
        "/v1/medications/parse-sig", json={"text": "metformin 500 mg PO BID"}, headers=headers
    ).json()
    assert parsed["frequency"]["code"] == "BID"
    assert parsed["rxnorm"] == "6809"


def test_operations_endpoints(client: TestClient, clinician_idp: FakeIdp) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json()["extraction"] == "rules"
    client.post(
        "/v1/verifications",
        json=submission(CLEAN_ENCOUNTER),
        headers=clinician_headers(clinician_idp),
    )
    metrics = client.get("/metrics").text
    assert "cdv_verifications_total" in metrics
    assert "cdv_http_request_duration_seconds" in metrics
    response = client.get("/healthz")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-correlation-id"]


def test_agent_card(client: TestClient) -> None:
    card: dict[str, Any] = client.get("/.well-known/agent-card.json").json()
    assert card["url"] == "https://cdv.test/a2a"
    assert card["skills"][0]["id"] == "verify-clinical-documentation"
    assert "agentIdentityFabric" in card["securitySchemes"]
    assert card["capabilities"]["streaming"] is False
