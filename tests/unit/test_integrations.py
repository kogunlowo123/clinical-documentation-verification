"""FHIR R4 client, chart mapping, SMART backend auth, resource builders and the fabric client."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from agentic_core.domain.classification import DataClassification
from cryptography.hazmat.primitives.asymmetric import ec

from clinical_documentation_verification.clinical.formulary import Formulary
from clinical_documentation_verification.domain.errors import (
    NotFoundError,
    UnauthorizedError,
    UpstreamError,
    UpstreamUnavailableError,
)
from clinical_documentation_verification.domain.models import Route
from clinical_documentation_verification.integrations.fhir import (
    FhirChartSource,
    FhirClient,
    FhirRecordWriter,
    NoAuth,
    SmartBackendAuth,
    _frequency,
    _strip_history,
    chart_from_resources,
    medication_sig,
)
from clinical_documentation_verification.integrations.identity import (
    AgentCredentialSource,
    IdentityFabricClient,
)
from tests.fakes import (
    FABRIC_URL,
    FHIR_BASE,
    SMART_TOKEN_URL,
    FakeClock,
    FakeFabric,
    FakeFhirServer,
    FakeSmartServer,
    medication_request,
)


async def _no_sleep(_: float) -> None:
    return None


def fhir_client(server: FakeFhirServer, auth: Any = None) -> FhirClient:
    return FhirClient(
        FHIR_BASE,
        http=httpx.AsyncClient(transport=httpx.MockTransport(server.handle)),
        auth=auth or NoAuth(),
        sleep=_no_sleep,
    )


class TestFhirClient:
    async def test_chart_snapshot_follows_paging(self, formulary: Formulary) -> None:
        server = FakeFhirServer(page_size=1)
        chart = await FhirChartSource(fhir_client(server), formulary).chart("pat-1001")
        assert {c.reference for c in chart.conditions} == {
            "Condition/cond-htn",
            "Condition/cond-dm2",
            "Condition/cond-asthma",
        }
        assert {m.name for m in chart.medications} == {"lisinopril", "atorvastatin", "aspirin"}
        lisinopril = next(m for m in chart.medications if m.name == "lisinopril")
        assert lisinopril.sig is not None
        assert lisinopril.sig.route is Route.PO
        assert lisinopril.sig.frequency.code == "DAILY"
        assert chart.allergies[0].substance == "Penicillin"
        assert "MedicationRequest/mr-lis/_history/2" in chart.versions
        pages = [r for r in server.requests if "_offset" in str(r.url)]
        assert pages  # more than one page was fetched

    async def test_unknown_patient(self, formulary: Formulary) -> None:
        with pytest.raises(NotFoundError, match="patient nobody"):
            await FhirChartSource(fhir_client(FakeFhirServer()), formulary).chart("nobody")

    async def test_paging_link_to_foreign_host_is_refused(self) -> None:
        server = FakeFhirServer(
            page_size=1, next_link_override="https://attacker.test/r4/Condition?page=2"
        )
        with pytest.raises(UpstreamError, match="outside the configured server"):
            await fhir_client(server).search("Condition", {"patient": "pat-1001"})

    async def test_page_cap(self) -> None:
        server = FakeFhirServer(page_size=1)
        client = FhirClient(
            FHIR_BASE,
            http=httpx.AsyncClient(transport=httpx.MockTransport(server.handle)),
            auth=NoAuth(),
            max_pages=2,
        )
        assert len(await client.search("Condition", {"patient": "pat-1001"})) == 2

    async def test_transient_errors_are_retried_once(self) -> None:
        server = FakeFhirServer(fail_gets=1)
        assert (await fhir_client(server).read("Patient", "pat-1001"))["id"] == "pat-1001"
        server.fail_gets = 2
        with pytest.raises(UpstreamUnavailableError):
            await fhir_client(server).read("Patient", "pat-1001")

    async def test_transport_errors(self) -> None:
        def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        client = FhirClient(
            FHIR_BASE,
            http=httpx.AsyncClient(transport=httpx.MockTransport(broken)),
            auth=NoAuth(),
            sleep=_no_sleep,
        )
        with pytest.raises(UpstreamUnavailableError, match="unreachable"):
            await client.read("Patient", "x")

    @pytest.mark.parametrize(
        ("status", "body", "error", "match"),
        [
            (
                400,
                {"resourceType": "OperationOutcome", "issue": [{"diagnostics": "bad param"}]},
                UpstreamError,
                "bad param",
            ),
            (
                403,
                {"resourceType": "OperationOutcome", "issue": [{"code": "forbidden"}]},
                UpstreamError,
                "forbidden",
            ),
            (422, {"other": 1}, UpstreamError, "422"),
            (429, {}, UpstreamUnavailableError, "429"),
        ],
    )
    async def test_error_mapping(
        self, status: int, body: dict[str, Any], error: type[Exception], match: str
    ) -> None:
        client = FhirClient(
            FHIR_BASE,
            http=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body))
            ),
            auth=NoAuth(),
        )
        with pytest.raises(error, match=match):
            await client.transaction({"resourceType": "Bundle", "type": "transaction", "entry": []})

    async def test_malformed_bodies(self) -> None:
        for response in (httpx.Response(200, text="<html>"), httpx.Response(200, json=[1])):
            client = FhirClient(
                FHIR_BASE,
                http=httpx.AsyncClient(transport=httpx.MockTransport(lambda _, r=response: r)),
                auth=NoAuth(),
            )
            with pytest.raises(UpstreamError):
                await client.read("Patient", "x")
        plain = FhirClient(
            FHIR_BASE,
            http=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(400, text="nope"))
            ),
            auth=NoAuth(),
        )
        with pytest.raises(UpstreamError, match=r"\(400\)$"):
            await plain.read("Patient", "x")

    async def test_record_writer_is_idempotent_by_identifier(self, clock: FakeClock) -> None:
        server = FakeFhirServer()
        writer = FhirRecordWriter(fhir_client(server), clock.now)
        doc = {
            "resourceType": "DocumentReference",
            "identifier": [{"system": "urn:ietf:rfc:3986", "value": "urn:uuid:abc"}],
        }
        prov = {"resourceType": "Provenance"}
        first = await writer.write(document_reference=doc, provenance=prov)
        second = await writer.write(document_reference=doc, provenance=prov)
        assert (
            first.document_reference
            == second.document_reference
            == "DocumentReference/documentreference-1"
        )
        assert first.provenance == "Provenance/provenance-2"
        bundle = server.transactions[0]
        assert bundle["entry"][1]["resource"]["target"] == [
            {"reference": bundle["entry"][0]["fullUrl"]}
        ]
        assert (
            bundle["entry"][0]["request"]["ifNoneExist"]
            == "identifier=urn:ietf:rfc:3986|urn:uuid:abc"
        )

    async def test_record_writer_rejects_malformed_responses(self, clock: FakeClock) -> None:
        for body in ({"entry": []}, {"entry": [{"response": {}}, {"response": {}}]}):
            client = FhirClient(
                FHIR_BASE,
                http=httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda _, b=body: httpx.Response(200, json=b))
                ),
                auth=NoAuth(),
            )
            with pytest.raises(UpstreamError):
                await FhirRecordWriter(client, clock.now).write(
                    document_reference={}, provenance={}
                )

    def test_strip_history(self) -> None:
        assert (
            _strip_history(f"{FHIR_BASE}/DocumentReference/9/_history/1", FHIR_BASE)
            == "DocumentReference/9"
        )
        assert _strip_history("weird", FHIR_BASE) == "weird"


class TestChartMapping:
    def test_structured_and_text_dosage(self, formulary: Formulary) -> None:
        structured = medication_request(
            "m1", "metformin 500 mg", "6809", dose=1000, route="26643006", per_day=2
        )
        sig = medication_sig(structured, "metformin", formulary)
        assert sig is not None
        assert sig.dose is not None
        assert sig.dose.value == 1000
        assert sig.frequency is not None
        assert sig.frequency.code == "BID"
        textual = {
            "dosageInstruction": [
                {
                    "text": "1 tab by mouth every 8 hours as needed",
                    "asNeededBoolean": True,
                    "route": {"text": "oral"},
                    "doseAndRate": [{"doseQuantity": {"value": 2, "code": "{puff}"}}],
                }
            ]
        }
        sig = medication_sig(textual, "albuterol", formulary)
        assert sig is not None
        assert sig.prn
        assert sig.route is Route.PO
        assert sig.dose.unit == "puff"
        assert medication_sig({}, "aspirin", formulary) is None
        assert medication_sig({}, "aspirin 81 mg", formulary) is not None

    def test_timing(self) -> None:
        for repeat, code in (
            ({"frequency": 1, "period": 6, "periodUnit": "h"}, "Q6H"),
            ({"frequency": 1, "period": 1, "periodUnit": "wk"}, "Q7D"),
            ({"frequency": 3, "period": 1, "periodUnit": "d"}, "TID"),
        ):
            parsed = _frequency({"repeat": repeat})
            assert parsed is not None
            assert parsed.code == code
        assert _frequency({"repeat": {"period": "x"}}) is None
        assert _frequency({"repeat": {"frequency": 0, "period": 1}}) is None
        assert _frequency({"code": {"text": "BID"}}).code == "BID"  # type: ignore[union-attr]
        assert _frequency({}) is None
        assert _frequency(None) is None

    def test_contained_medication_and_exclusions(self, formulary: Formulary) -> None:
        contained = {
            "resourceType": "MedicationRequest",
            "id": "m9",
            "status": "active",
            "contained": [
                {
                    "resourceType": "Medication",
                    "id": "med1",
                    "code": {
                        "coding": [
                            {
                                "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                "code": "1364430",
                                "display": "Eliquis 5 MG",
                            }
                        ]
                    },
                }
            ],
            "medicationReference": {"reference": "#med1"},
        }
        external = {
            "resourceType": "MedicationRequest",
            "id": "m10",
            "status": "active",
            "medicationReference": {"reference": "Medication/1"},
        }
        refuted = {
            "resourceType": "Condition",
            "id": "c1",
            "code": {"text": "Gout"},
            "verificationStatus": {"coding": [{"code": "refuted"}]},
        }
        unnamed = {
            "resourceType": "Condition",
            "id": "c2",
            "code": {"coding": [{"system": "x", "code": "1"}]},
        }
        resolved_allergy = {
            "resourceType": "AllergyIntolerance",
            "id": "a1",
            "code": {"text": "latex"},
            "clinicalStatus": {"coding": [{"code": "resolved"}]},
        }
        entered = {
            "resourceType": "AllergyIntolerance",
            "id": "a2",
            "code": {"text": "sulfa"},
            "verificationStatus": {"coding": [{"code": "entered-in-error"}]},
        }
        coded = {
            "resourceType": "AllergyIntolerance",
            "id": "a3",
            "code": {
                "coding": [
                    {
                        "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                        "code": "723",
                        "display": "amoxicillin",
                    }
                ]
            },
        }
        chart = chart_from_resources(
            "p",
            [refuted, unnamed],
            [contained, external],
            [resolved_allergy, entered, coded],
            formulary,
        )
        assert [c.name for c in chart.conditions] == ["unnamed condition"]
        assert chart.medications[0].rxnorm == "1364430"
        assert chart.medications[0].name == "Eliquis 5 MG"
        assert chart.medications[1].name == "unnamed medication"
        assert [(a.substance, a.rxnorm) for a in chart.allergies] == [("amoxicillin", "723")]


class TestSmartBackendAuth:
    def make(self, smart: FakeSmartServer, clock: FakeClock) -> SmartBackendAuth:
        key = ec.generate_private_key(ec.SECP384R1())
        smart.public_key = key.public_key()
        return SmartBackendAuth(
            token_url=SMART_TOKEN_URL,
            client_id="cdv-backend",
            private_key=key,
            key_id="cdv-2026-10",
            algorithm="ES384",
            scope="system/Patient.read",
            http=httpx.AsyncClient(transport=httpx.MockTransport(smart.handle)),
            clock=clock.now,
        )

    async def test_token_is_cached_and_refreshed(self, clock: FakeClock) -> None:
        smart = FakeSmartServer()
        auth = self.make(smart, clock)
        assert await auth.headers() == {"Authorization": "Bearer fhir-at-1"}
        assert await auth.headers() == {"Authorization": "Bearer fhir-at-1"}
        assert smart.last_assertion is not None
        assert smart.last_assertion["aud"] == SMART_TOKEN_URL
        assert smart.last_assertion["exp"] - smart.last_assertion["iat"] == 300
        clock.advance(271)
        assert await auth.headers() == {"Authorization": "Bearer fhir-at-2"}

    async def test_bearer_reaches_fhir(self, clock: FakeClock) -> None:
        smart = FakeSmartServer()
        server = FakeFhirServer(require_token="fhir-at-1")
        client = fhir_client(server, auth=self.make(smart, clock))
        assert (await client.read("Patient", "pat-1001"))["id"] == "pat-1001"

    async def test_failures(self, clock: FakeClock) -> None:
        smart = FakeSmartServer(fail=True)
        with pytest.raises(UpstreamError):
            await self.make(smart, clock).headers()
        smart = FakeSmartServer(client_id="someone-else")
        with pytest.raises(UpstreamError, match="401"):
            await self.make(smart, clock).headers()
        bad = self.make(FakeSmartServer(), clock)
        bad._http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"token": 1}))
        )
        with pytest.raises(UpstreamError, match="malformed"):
            await bad.headers()

        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("t", request=request)

        bad._http = httpx.AsyncClient(transport=httpx.MockTransport(down))
        with pytest.raises(UpstreamUnavailableError):
            await bad.headers()


class TestIdentityFabricClient:
    def client(self, fabric: FakeFabric) -> IdentityFabricClient:
        return IdentityFabricClient(
            FABRIC_URL, http=httpx.AsyncClient(transport=httpx.MockTransport(fabric.handle))
        )

    async def test_wire_calls(self) -> None:
        fabric = FakeFabric()
        token = fabric.credential(
            "spiffe://agents.test/agent/clinical/scribe", "documentation:verify", act={"sub": "x"}
        )
        client = self.client(fabric)
        info = await client.introspect(token)
        assert info.active
        assert info.sub.endswith("/scribe")
        assert info.act == {"sub": "x"}
        assert (await client.introspect("unknown")).active is False
        decision = await client.authorize(
            token,
            action="documentation:verify",
            resource_type="Patient",
            resource_id="p1",
            classification=DataClassification.RESTRICTED,
            attributes={},
            environment={"purpose_of_use": "TREAT"},
        )
        assert decision.decision.value == "allow"
        assert fabric.authorize_calls[0]["resource_classification"] == 3
        issued = await client.token(
            client_id="spiffe://agents.test/agent/clinical/cdv",
            assertion="projected-sa-token",
            scope="fhir:write:DocumentReference",
        )
        assert issued.expires_in == 600

    async def test_errors(self) -> None:
        fabric = FakeFabric()
        client = self.client(fabric)
        with pytest.raises(UnauthorizedError):
            await client.authorize(
                "nope",
                action="a",
                resource_type="t",
                resource_id="i",
                classification=DataClassification.INTERNAL,
                attributes={},
                environment={},
            )
        with pytest.raises(UpstreamError, match="invalid_grant"):
            await client.token(client_id="x", assertion="wrong")
        fabric.down = True
        with pytest.raises(UpstreamUnavailableError):
            await client.introspect("t")
        for response in (
            httpx.Response(200, text="x"),
            httpx.Response(200, json=[1]),
            httpx.Response(200, json={"active": "maybe"}),
            httpx.Response(404, text="x"),
        ):
            odd = IdentityFabricClient(
                FABRIC_URL,
                http=httpx.AsyncClient(transport=httpx.MockTransport(lambda _, r=response: r)),
            )
            with pytest.raises(UpstreamError):
                await odd.introspect("t")

        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("x", request=request)

        unreachable = IdentityFabricClient(
            FABRIC_URL, http=httpx.AsyncClient(transport=httpx.MockTransport(down))
        )
        with pytest.raises(UpstreamUnavailableError):
            await unreachable.introspect("t")

    async def test_own_credential_is_cached_until_expiry(
        self, tmp_path: Path, clock: FakeClock
    ) -> None:
        fabric = FakeFabric()
        token_file = tmp_path / "token"
        token_file.write_text("projected-sa-token\n", encoding="utf-8")
        source = AgentCredentialSource(
            self.client(fabric),
            agent_id="spiffe://agents.test/agent/clinical/cdv",
            workload_token_file=token_file,
            clock=clock.now,
        )
        first = await source.credential()
        assert await source.credential() == first
        clock.advance(541)
        assert await source.credential() != first
        assert fabric.tokens_issued == 2
        assert source.agent_id.endswith("/cdv")


def test_document_builders(formulary: Formulary) -> None:
    from clinical_documentation_verification.integrations.fhir_documents import (
        document_reference,
        provenance,
    )
    from tests.unit.test_service import build_verification

    v = build_verification(formulary)
    at = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    doc = document_reference(v, note_text="Signed note", practitioner="Practitioner/1", at=at)
    data = base64.b64decode(doc["content"][0]["attachment"]["data"])
    assert data == b"Signed note"
    assert (
        doc["content"][0]["attachment"]["hash"]
        == base64.b64encode(hashlib.sha1(data, usedforsecurity=False).digest()).decode()
    )
    assert doc["type"]["coding"][0]["code"] == v.note_type
    assert doc["context"]["encounter"] == [{"reference": f"Encounter/{v.encounter_id}"}]
    prov = provenance(v, practitioner="Practitioner/1", agent_id="spiffe://a/agent/x/y", at=at)
    assert prov["signature"][0]["who"] == {"reference": "Practitioner/1"}
    assert prov["entity"][0]["role"] == "derivation"
