"""Application service: workflow, sign-off state machine, policy, model cross-check, fabric mode."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from agentic_core.policy import PolicyEngine, PolicySet

from clinical_documentation_verification.application.commands import (
    ApproveVerification,
    ProposedOrder,
    RejectVerification,
    SubmitVerification,
    ValidateOrders,
)
from clinical_documentation_verification.application.service import VerificationService
from clinical_documentation_verification.clinical.consistency import analyse
from clinical_documentation_verification.clinical.extraction import DeterministicExtractor
from clinical_documentation_verification.clinical.formulary import Formulary
from clinical_documentation_verification.clinical.segmentation import segment
from clinical_documentation_verification.domain.errors import (
    ConflictError,
    ForbiddenError,
    InvalidStateError,
    NotFoundError,
    UpstreamUnavailableError,
    VerificationError,
)
from clinical_documentation_verification.domain.models import (
    PolicyOutcome,
    TranscriptInput,
    Verification,
    VerificationStatus,
)
from clinical_documentation_verification.integrations.fhir import chart_from_resources
from clinical_documentation_verification.runtime.container import Container
from clinical_documentation_verification.security.principals import Principal
from tests.conftest import (
    CLEAN_ENCOUNTER,
    ENCOUNTER,
    PRACTITIONER,
    ROOT,
    make_container,
    make_settings,
    model_answer,
)
from tests.fakes import FakeAgentIssuer, FakeClock, FakeIdp, FakeModel, Upstreams, synthetic_chart

ATTEST = ApproveVerification(statement="I reviewed and verified this documentation.")


def build_verification(formulary: Formulary) -> Verification:
    transcript = segment(TranscriptInput(text=CLEAN_ENCOUNTER))
    extraction = DeterministicExtractor(formulary).extract(transcript)
    resources = synthetic_chart()
    chart = chart_from_resources(
        "pat-1001",
        list(resources["Condition"].values()),
        list(resources["MedicationRequest"].values()),
        list(resources["AllergyIntolerance"].values()),
        formulary,
    )
    now = datetime(2026, 10, 14, tzinfo=UTC)
    return Verification(
        id=str(uuid.uuid4()),
        patient_id="pat-1001",
        encounter_id="enc-1",
        note_type="11506-3",
        status=VerificationStatus.PENDING_APPROVAL,
        submitted_by="tester",
        created_at=now,
        updated_at=now,
        transcript=transcript,
        extraction=extraction,
        chart=chart,
        diff=(),
        findings=(),
        consistency=analyse(extraction, reference=None, diff=(), findings=(), formulary=formulary),
        draft_note="draft",
        write_policy=PolicyOutcome(
            action="fhir:write:DocumentReference",
            decision="require_approval",
            matched_rules=("approve-chart-writes",),
            policy_version="1",
            policy_digest="sha256:x",
            reason="human approval required",
        ),
    )


def clinician(
    *roles: str, fhir_user: str | None = PRACTITIONER, subject: str = "dr.okafor"
) -> Principal:
    return Principal(
        subject=subject,
        kind="user",
        roles=frozenset(roles or ("physician",)),
        fhir_user=fhir_user,
        via="oidc",
        token="t",
    )


def agent(
    scopes: str = "documentation:verify documentation:read orders:validate",
    *,
    delegated: bool = False,
    via: str = "jwks",
    token: str = "agent-credential",  # noqa: S107 - opaque test credential
) -> Principal:
    return Principal(
        subject="spiffe://agents.test/agent/clinical/scribe",
        kind="agent",
        scopes=frozenset(scopes.split()),
        delegated=delegated,
        via=via,
        token=token,  # type: ignore[arg-type]
    )


def submit_cmd(text: str = CLEAN_ENCOUNTER, **kw: object) -> SubmitVerification:
    return SubmitVerification(
        patient_id="pat-1001",
        encounter_id="enc-9",
        transcript=TranscriptInput(text=text),
        **kw,  # type: ignore[arg-type]
    )


class TestWorkflow:
    async def test_submit_approve_write(
        self, service: VerificationService, upstreams: Upstreams
    ) -> None:
        v = await service.submit(submit_cmd(note_type="34117-2"), agent())
        assert v.status is VerificationStatus.PENDING_APPROVAL
        assert v.note_type == "34117-2"
        assert v.draft_note.startswith("HISTORY AND PHYSICAL NOTE")
        assert v.write_policy.decision == "require_approval"
        assert v.chart.allergies
        assert v.reference_extraction is None
        written = await service.approve(v.id, ATTEST, clinician())
        assert written.status is VerificationStatus.WRITTEN
        assert written.version == v.version + 2
        assert written.clinician_decision is not None
        assert written.clinician_decision.note_digest.startswith("sha256:")  # type: ignore[union-attr]
        assert upstreams.fhir.written("DocumentReference")[0]["subject"] == {
            "reference": "Patient/pat-1001"
        }
        assert (await service.approve(v.id, ATTEST, clinician())).write == written.write

    async def test_proposed_orders_are_validated(self, service: VerificationService) -> None:
        v = await service.submit(
            submit_cmd(proposed_orders=(ProposedOrder(text="amoxicillin 500 mg PO TID"),)),
            clinician("medical-scribe"),
        )
        assert any(f.rule == "allergy-contraindication" for f in v.findings)
        assert v.consistency.band.value == "inconsistent"
        assert v.extraction.orders[-1].source == "proposed"

    async def test_reject_and_state_rules(self, service: VerificationService) -> None:
        v = await service.submit(submit_cmd(), clinician())
        rejected = await service.reject(
            v.id, RejectVerification(reason="Wrong patient on this transcript."), clinician()
        )
        assert rejected.status is VerificationStatus.REJECTED
        assert rejected.clinician_decision.decision == "rejected"  # type: ignore[union-attr]
        with pytest.raises(InvalidStateError):
            await service.approve(v.id, ATTEST, clinician())
        with pytest.raises(InvalidStateError):
            await service.reject(
                v.id, RejectVerification(reason="Again, should not work."), clinician()
            )
        with pytest.raises(NotFoundError):
            await service.get(str(uuid.uuid4()), clinician())

    async def test_list_filters(self, service: VerificationService) -> None:
        first = await service.submit(submit_cmd(), clinician())
        await service.submit(submit_cmd(), clinician())
        await service.reject(
            first.id, RejectVerification(reason="Duplicate submission of encounter."), clinician()
        )
        assert (
            len(
                await service.list(
                    clinician(), patient_id="pat-1001", status=None, limit=10, offset=0
                )
            )
            == 2
        )
        rejected = await service.list(
            clinician(), patient_id=None, status=VerificationStatus.REJECTED, limit=10, offset=0
        )
        assert [v.id for v in rejected] == [first.id]

    async def test_idempotency(self, service: VerificationService) -> None:
        first = await service.submit(submit_cmd(), agent(), idempotency_key="key-000001")
        assert (
            await service.submit(submit_cmd(), agent(), idempotency_key="key-000001")
        ).id == first.id
        other = SubmitVerification(
            patient_id="pat-1001",
            encounter_id="enc-other",
            transcript=TranscriptInput(text=CLEAN_ENCOUNTER),
        )
        with pytest.raises(ConflictError):
            await service.submit(other, agent(), idempotency_key="key-000001")

    async def test_empty_transcript(self, service: VerificationService) -> None:
        with pytest.raises(VerificationError, match="no utterances"):
            await service.submit(submit_cmd(text="   \n  "), clinician())

    async def test_order_tools(self, service: VerificationService) -> None:
        result = await service.validate_orders(
            ValidateOrders(
                orders=(ProposedOrder(text="lisinopril 10 mg PO daily"),),
                allergies=("ace inhibitor",),
            ),
            agent(),
        )
        assert result.chart_checked is False
        assert result.safe_to_sign is False
        assert (await service.parse("aspirin 81 mg PO daily", agent())).rxnorm == "1191"


class TestPolicyEnforcement:
    async def test_who_may_approve(self, service: VerificationService) -> None:
        v = await service.submit(submit_cmd(), clinician())
        for who, rule in (
            (agent(), "deny-agent-signoff"),
            (clinician("registered-nurse"), "default deny"),
            (clinician(fhir_user="Patient/pat-1001"), "deny-signoff-without-practitioner-identity"),
            (clinician(fhir_user=None), "deny-signoff-without-fhir-user"),
        ):
            with pytest.raises(ForbiddenError, match=rule):
                await service.approve(v.id, ATTEST, who)

    async def test_override_is_required_for_critical_findings(
        self, service: VerificationService
    ) -> None:
        v = await service.submit(submit_cmd(ENCOUNTER), clinician())
        with pytest.raises(ForbiddenError, match="critical"):
            await service.approve(v.id, ATTEST, clinician())
        signed = await service.approve(
            v.id,
            ApproveVerification(
                statement=ATTEST.statement, override_reason="Order cancelled before signing."
            ),
            clinician("physician-assistant"),
        )
        assert signed.status is VerificationStatus.WRITTEN

    async def test_agent_scopes_and_delegation(self, service: VerificationService) -> None:
        with pytest.raises(ForbiddenError, match="default deny"):
            await service.submit(submit_cmd(), agent("documentation:read"))
        with pytest.raises(ForbiddenError, match="deny-delegated-agent-submission"):
            await service.submit(submit_cmd(), agent(delegated=True))
        with pytest.raises(ForbiddenError):
            await service.list(
                agent("orders:validate"), patient_id=None, status=None, limit=1, offset=0
            )

    async def test_write_requires_service_permission(
        self,
        tmp_path: Path,
        upstreams: Upstreams,
        clinician_idp: FakeIdp,
        agent_issuer: FakeAgentIssuer,
        clock: FakeClock,
    ) -> None:
        document = yaml.safe_load(
            (ROOT / "policies" / "documentation-verification.yaml").read_text(encoding="utf-8")
        )
        document["rules"] = [
            r for r in document["rules"] if r["id"] != "allow-chart-write-after-signature"
        ]
        strict = tmp_path / "strict.yaml"
        strict.write_text(yaml.safe_dump(document), encoding="utf-8")
        PolicyEngine(PolicySet.from_file(strict))
        container = await make_container(
            make_settings(tmp_path, policy_file=strict),
            upstreams,
            clinician_idp,
            agent_issuer,
            clock,
        )
        try:
            v = await container.service.submit(submit_cmd(), clinician())
            with pytest.raises(ForbiddenError, match="chart write not permitted"):
                await container.service.approve(v.id, ATTEST, clinician())
            stored = await container.service.get(v.id, clinician())
            assert stored.status is VerificationStatus.APPROVED
            assert upstreams.fhir.transactions == []
        finally:
            await container.aclose()


class TestModelExtraction:
    async def test_model_extraction_is_cross_checked(
        self,
        tmp_path: Path,
        upstreams: Upstreams,
        clinician_idp: FakeIdp,
        agent_issuer: FakeAgentIssuer,
        clock: FakeClock,
    ) -> None:
        model = FakeModel(
            answers=[
                model_answer(
                    medications=[
                        {
                            "name": "lisinopril",
                            "sig_text": "10 mg daily",
                            "action": "continue",
                            "evidence": [1, 4],
                        },
                        {
                            "name": "warfarin",
                            "sig_text": "5 mg daily",
                            "action": "continue",
                            "evidence": [1],
                        },
                    ]
                )
            ]
        )
        container = await make_container(
            make_settings(tmp_path), upstreams, clinician_idp, agent_issuer, clock, model=model
        )
        try:
            assert container.service.model_extraction_enabled
            v = await container.service.submit(submit_cmd(), clinician())
            assert v.extraction.source == "model"
            assert v.reference_extraction is not None
            assert "agreement" in v.consistency.components
            assert any(f.rule == "ungrounded-item" and f.item == "warfarin" for f in v.findings)
        finally:
            await container.aclose()

    async def test_fallback_when_model_fails(
        self,
        tmp_path: Path,
        upstreams: Upstreams,
        clinician_idp: FakeIdp,
        agent_issuer: FakeAgentIssuer,
        clock: FakeClock,
    ) -> None:
        model = FakeModel(answers=["oops", "still not json", "nope"])
        container = await make_container(
            make_settings(tmp_path), upstreams, clinician_idp, agent_issuer, clock, model=model
        )
        try:
            v = await container.service.submit(submit_cmd(), clinician())
            assert v.extraction.source == "deterministic"
            assert {"model-output-invalid", "model-extraction-fallback"} <= {
                f.rule for f in v.findings
            }
        finally:
            await container.aclose()


class TestFabricMode:
    async def make(
        self,
        tmp_path: Path,
        upstreams: Upstreams,
        clinician_idp: FakeIdp,
        agent_issuer: FakeAgentIssuer,
        clock: FakeClock,
    ) -> Container:
        token_file = tmp_path / "sa-token"
        token_file.write_text("projected-sa-token", encoding="utf-8")
        settings = make_settings(
            tmp_path,
            fabric_url="https://fabric.test",
            workload_token_file=token_file,
            agent_issuer=None,
            agent_jwks_url=None,
        )
        return await make_container(settings, upstreams, clinician_idp, agent_issuer, clock)

    async def test_fabric_decisions_are_combined(
        self,
        tmp_path: Path,
        upstreams: Upstreams,
        clinician_idp: FakeIdp,
        agent_issuer: FakeAgentIssuer,
        clock: FakeClock,
    ) -> None:
        container = await self.make(tmp_path, upstreams, clinician_idp, agent_issuer, clock)
        try:
            token = upstreams.fabric.credential(
                "spiffe://agents.test/agent/clinical/scribe",
                "documentation:verify documentation:read",
            )
            principal = await container.authenticator.authenticate(f"Bearer {token}")
            assert principal.via == "fabric"
            v = await container.service.submit(submit_cmd(), principal)
            assert upstreams.fabric.authorize_calls[0]["environment"]["purpose_of_use"] == "TREAT"
            upstreams.fabric.decide = lambda _sub, body: (
                "deny" if body["action"] == "documentation:read" else "allow"
            )
            with pytest.raises(ForbiddenError, match="identity fabric"):
                await container.service.get(v.id, principal)

            written = await container.service.approve(v.id, ATTEST, clinician())
            assert written.status is VerificationStatus.WRITTEN
            assert upstreams.fabric.tokens_issued == 1  # the service's own credential
            write_call = upstreams.fabric.authorize_calls[-1]
            assert write_call["action"] == "fhir:write:DocumentReference"
            assert write_call["environment"]["clinician_approved"] is True
        finally:
            await container.aclose()

    async def test_fabric_require_approval_blocks_the_write(
        self,
        tmp_path: Path,
        upstreams: Upstreams,
        clinician_idp: FakeIdp,
        agent_issuer: FakeAgentIssuer,
        clock: FakeClock,
    ) -> None:
        container = await self.make(tmp_path, upstreams, clinician_idp, agent_issuer, clock)
        try:
            upstreams.fabric.decide = lambda _sub, body: (
                "require_approval" if body["action"].startswith("fhir:") else "allow"
            )
            v = await container.service.submit(submit_cmd(), clinician())
            with pytest.raises(ForbiddenError, match="identity fabric"):
                await container.service.approve(v.id, ATTEST, clinician())
            upstreams.fabric.down = True
            with pytest.raises(UpstreamUnavailableError):
                await container.authenticator.authenticate("Bearer " + agent_issuer.mint())
        finally:
            await container.aclose()
