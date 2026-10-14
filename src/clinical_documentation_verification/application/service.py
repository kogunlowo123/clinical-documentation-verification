"""Documentation verification use cases.

Workflow: Transcript -> Clinical Extraction -> Chart Review -> Orders
Validation -> Consistency Analysis -> Authorized Update.

Nothing reaches the chart without a clinician signature: submission always
ends in ``pending_approval`` with a recorded ``require_approval`` policy
decision for the chart write; approval re-evaluates policy as the signing
practitioner, and only then is a DocumentReference plus Provenance written
in one FHIR transaction. Every decision and state change is in the signed
audit chain.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from typing import Any

from agentic_core.audit import AuditLog
from agentic_core.domain.classification import DataClassification, Decision
from agentic_core.observability import correlation_id, get_logger
from agentic_core.policy import Resource, Subject

from clinical_documentation_verification.application.commands import (
    ApproveVerification,
    OrderValidationResult,
    RejectVerification,
    SubmitVerification,
    ValidateOrders,
)
from clinical_documentation_verification.clinical.consistency import analyse
from clinical_documentation_verification.clinical.extraction import DeterministicExtractor
from clinical_documentation_verification.clinical.formulary import Formulary
from clinical_documentation_verification.clinical.grounding import Grounder
from clinical_documentation_verification.clinical.note import render_note
from clinical_documentation_verification.clinical.orders import OrderValidator, stopped_medications
from clinical_documentation_verification.clinical.reconciliation import ChartReconciler
from clinical_documentation_verification.clinical.segmentation import segment
from clinical_documentation_verification.clinical.sig import parse_sig
from clinical_documentation_verification.domain.errors import (
    ConflictError,
    ForbiddenError,
    InvalidStateError,
    NotFoundError,
    VerificationError,
)
from clinical_documentation_verification.domain.models import (
    ClinicalExtraction,
    ClinicianDecision,
    Finding,
    MedicationSig,
    OrderItem,
    OrderKind,
    Severity,
    Verification,
    VerificationStatus,
)
from clinical_documentation_verification.domain.ports import (
    ChartSource,
    Clock,
    RecordWriter,
    VerificationRepository,
)
from clinical_documentation_verification.integrations.fhir_documents import (
    NOTE_TYPES,
    document_reference,
    provenance,
)
from clinical_documentation_verification.integrations.identity import AgentCredentialSource
from clinical_documentation_verification.observability.metrics import VerificationMetrics
from clinical_documentation_verification.orchestration.extraction_agent import ExtractionAgent
from clinical_documentation_verification.security.authorization import AccessController
from clinical_documentation_verification.security.principals import Principal

WRITE_ACTION = "fhir:write:DocumentReference"
log = get_logger("clinical_documentation_verification.service")


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class VerificationService:
    def __init__(
        self,
        *,
        repository: VerificationRepository,
        chart: ChartSource,
        writer: RecordWriter,
        access: AccessController,
        audit: AuditLog,
        formulary: Formulary,
        metrics: VerificationMetrics,
        clock: Clock,
        agent_id: str,
        default_note_type: str = "11506-3",
        extraction_agent: ExtractionAgent | None = None,
        credentials: AgentCredentialSource | None = None,
    ) -> None:
        self._repo = repository
        self._chart = chart
        self._writer = writer
        self._access = access
        self._audit = audit
        self._formulary = formulary
        self._metrics = metrics
        self._clock = clock
        self._agent_id = agent_id
        self._default_note_type = default_note_type
        self._agent = extraction_agent
        self._credentials = credentials
        self._deterministic = DeterministicExtractor(formulary)
        self._grounder = Grounder(formulary)
        self._reconciler = ChartReconciler(formulary)
        self._validator = OrderValidator(formulary)

    @property
    def model_extraction_enabled(self) -> bool:
        return self._agent is not None

    # ------------------------------------------------------------------ submission

    async def submit(
        self,
        command: SubmitVerification,
        principal: Principal,
        *,
        idempotency_key: str | None = None,
    ) -> Verification:
        await self._access.require(
            principal,
            "documentation:verify",
            resource_type="Patient",
            resource_id=command.patient_id,
        )
        if idempotency_key:
            existing = await self._repo.find_by_idempotency_key(idempotency_key)
            if existing is not None:
                return self._replay(existing, command)

        transcript = segment(command.transcript)
        if not transcript.utterances:
            raise VerificationError("transcript contains no utterances")

        reference = self._deterministic.extract(transcript)
        extraction, findings, cross_check = reference, list[Finding](), None
        if self._agent is not None:
            run = await self._agent.extract(transcript)
            findings.extend(run.findings)
            if run.extraction is not None:
                extraction = self._grounder.ground(run.extraction, transcript)
                cross_check = reference
                self._metrics.extraction_attempts.labels("success").inc()
            else:
                findings.append(
                    Finding(
                        rule="model-extraction-fallback",
                        severity=Severity.INFO,
                        message="rule-based extraction used because model extraction did not "
                        "complete",
                    )
                )
                self._metrics.extraction_attempts.labels("fallback").inc()

        proposed = tuple(
            OrderItem(
                kind=OrderKind.MEDICATION,
                description=order.text,
                medication=parse_sig(order.text, self._formulary),
                source="proposed",
            )
            for order in command.proposed_orders
        )
        extraction = extraction.model_copy(update={"orders": (*extraction.orders, *proposed)})
        findings.extend(_ungrounded(extraction))

        chart = await self._chart.chart(command.patient_id)
        diff = self._reconciler.diff(extraction, chart)
        findings.extend(
            self._validator.validate(
                extraction.orders,
                chart=chart,
                allergies=[a.substance for a in extraction.allergies],
                stopped=stopped_medications(extraction.medications),
            )
        )
        consistency = analyse(
            extraction,
            reference=cross_check,
            diff=diff,
            findings=findings,
            formulary=self._formulary,
        )
        note_type = command.note_type or self._default_note_type
        draft = render_note(
            note_title=NOTE_TYPES[note_type],
            transcript=transcript,
            extraction=extraction,
            diff=diff,
            findings=findings,
            consistency=consistency,
        )
        verification_id = str(uuid.uuid4())
        write_policy = await self._access.decide(
            self._service_subject(),
            WRITE_ACTION,
            self._write_resource(verification_id, consistency.critical_findings),
            {"clinician_approved": False},
        )
        now = self._clock.now()
        verification = Verification(
            id=verification_id,
            patient_id=command.patient_id,
            encounter_id=command.encounter_id,
            note_type=note_type,
            status=VerificationStatus.PENDING_APPROVAL,
            submitted_by=principal.subject,
            created_at=now,
            updated_at=now,
            transcript=transcript,
            extraction=extraction,
            reference_extraction=cross_check,
            chart=chart,
            diff=diff,
            findings=tuple(findings),
            consistency=consistency,
            draft_note=draft,
            write_policy=write_policy,
            idempotency_key=idempotency_key,
        )
        try:
            await self._repo.add(verification)
        except ConflictError:
            if idempotency_key:
                existing = await self._repo.find_by_idempotency_key(idempotency_key)
                if existing is not None:
                    return self._replay(existing, command)
            raise
        await self._record(
            principal.subject,
            "verification.submit",
            verification,
            "success",
            {
                "source": extraction.source,
                "band": consistency.band.value,
                "score": consistency.score,
                "findings": len(findings),
                "critical": consistency.critical_findings,
                "write_policy": write_policy.decision,
                "transcript_digest": transcript.digest,
            },
        )
        self._observe(verification)
        return verification

    @staticmethod
    def _replay(existing: Verification, command: SubmitVerification) -> Verification:
        if (existing.patient_id, existing.encounter_id) != (
            command.patient_id,
            command.encounter_id,
        ):
            raise ConflictError("idempotency key was already used for a different encounter")
        return existing

    def _observe(self, v: Verification) -> None:
        self._metrics.verifications.labels(v.consistency.band.value, v.extraction.source).inc()
        self._metrics.consistency.observe(v.consistency.score)
        for f in v.findings:
            self._metrics.findings.labels(f.rule, f.severity.value).inc()
        for d in v.diff:
            self._metrics.chart_diff.labels(d.category, d.kind.value).inc()

    # --------------------------------------------------------------------- reads

    async def get(self, verification_id: str, principal: Principal) -> Verification:
        verification = await self._load(verification_id)
        await self._access.require(
            principal,
            "documentation:read",
            resource_type="verification",
            resource_id=verification_id,
            attributes={"patient_id": verification.patient_id},
        )
        return verification

    async def list(
        self,
        principal: Principal,
        *,
        patient_id: str | None,
        status: VerificationStatus | None,
        limit: int,
        offset: int,
    ) -> Sequence[Verification]:
        await self._access.require(
            principal,
            "documentation:read",
            resource_type="verification",
            resource_id=patient_id or "*",
        )
        return await self._repo.list(
            patient_id=patient_id, status=status, limit=limit, offset=offset
        )

    async def _load(self, verification_id: str) -> Verification:
        verification = await self._repo.get(verification_id)
        if verification is None:
            raise NotFoundError(f"verification {verification_id} not found")
        return verification

    # ----------------------------------------------------------------- sign-off

    def _signoff_attributes(self, v: Verification) -> dict[str, Any]:
        return {
            "status": v.status.value,
            "band": v.consistency.band.value,
            "critical_findings": v.consistency.critical_findings,
            "patient_id": v.patient_id,
        }

    async def approve(
        self, verification_id: str, command: ApproveVerification, principal: Principal
    ) -> Verification:
        verification = await self._load(verification_id)
        environment = (
            {"override_reason": command.override_reason} if command.override_reason else {}
        )
        outcome = await self._access.require(
            principal,
            "documentation:approve",
            resource_type="verification",
            resource_id=verification_id,
            attributes=self._signoff_attributes(verification),
            environment=environment,
        )
        if outcome.decision != Decision.ALLOW.value:
            raise ForbiddenError(f"approval not permitted: {outcome.reason}")
        if verification.status is VerificationStatus.WRITTEN:
            return verification
        if verification.status is VerificationStatus.REJECTED:
            raise InvalidStateError(f"verification {verification_id} was rejected")
        if verification.status is VerificationStatus.PENDING_APPROVAL:
            now = self._clock.now()
            practitioner = principal.fhir_user or ""
            body = command.amended_note or verification.draft_note
            final_note = (
                f"{body.rstrip()}\n\nATTESTATION\n{command.statement}\n"
                f"Electronically signed by {practitioner} at {now.isoformat()}\n"
            )
            decision = ClinicianDecision(
                subject=principal.subject,
                practitioner=practitioner,
                decision="approved",
                at=now,
                statement=command.statement,
                override_reason=command.override_reason,
                final_note=final_note,
                note_digest=_digest(final_note),
                policy=outcome,
            )
            approved = verification.transition(VerificationStatus.APPROVED, at=now).model_copy(
                update={"clinician_decision": decision}
            )
            await self._repo.update(approved, expected_version=verification.version)
            await self._record(
                principal.subject,
                "verification.approve",
                approved,
                "success",
                {
                    "practitioner": practitioner,
                    "override": command.override_reason is not None,
                    "amended": command.amended_note is not None,
                    "note_digest": decision.note_digest,
                    "policy_digest": outcome.policy_digest,
                },
            )
            self._metrics.decisions.labels(
                "approved", str(command.override_reason is not None).lower()
            ).inc()
            verification = approved
        return await self._write(verification)

    async def _write(self, verification: Verification) -> Verification:
        decision = verification.clinician_decision
        if decision is None or decision.final_note is None:  # pragma: no cover - state invariant
            raise InvalidStateError("approved verification has no signed note")
        token = await self._credentials.credential() if self._credentials else None
        policy = await self._access.decide(
            self._service_subject(),
            WRITE_ACTION,
            self._write_resource(verification.id, verification.consistency.critical_findings),
            {"clinician_approved": True, "approver": decision.practitioner},
            fabric_token=token,
        )
        if policy.decision != Decision.ALLOW.value:
            self._metrics.fhir_writes.labels("denied").inc()
            await self._record(
                self._agent_id, "fhir.write", verification, "denied", {"reason": policy.reason}
            )
            raise ForbiddenError(f"chart write not permitted: {policy.reason}")
        now = self._clock.now()
        try:
            result = await self._writer.write(
                document_reference=document_reference(
                    verification,
                    note_text=decision.final_note,
                    practitioner=decision.practitioner,
                    at=now,
                ),
                provenance=provenance(
                    verification,
                    practitioner=decision.practitioner,
                    agent_id=self._agent_id,
                    at=now,
                ),
            )
        except VerificationError as exc:
            self._metrics.fhir_writes.labels("failed").inc()
            await self._record(
                self._agent_id, "fhir.write", verification, "failure", {"error": exc.code}
            )
            raise
        written = verification.transition(VerificationStatus.WRITTEN, at=now).model_copy(
            update={"write": result}
        )
        await self._repo.update(written, expected_version=verification.version)
        self._metrics.fhir_writes.labels("written").inc()
        await self._record(
            self._agent_id,
            "fhir.write",
            written,
            "success",
            {
                "document_reference": result.document_reference,
                "provenance": result.provenance,
                "approved_by": decision.practitioner,
                "policy_digest": policy.policy_digest,
            },
        )
        return written

    async def reject(
        self, verification_id: str, command: RejectVerification, principal: Principal
    ) -> Verification:
        verification = await self._load(verification_id)
        outcome = await self._access.require(
            principal,
            "documentation:reject",
            resource_type="verification",
            resource_id=verification_id,
            attributes=self._signoff_attributes(verification),
        )
        if verification.status is not VerificationStatus.PENDING_APPROVAL:
            raise InvalidStateError(f"verification {verification_id} is {verification.status}")
        now = self._clock.now()
        rejected = verification.transition(VerificationStatus.REJECTED, at=now).model_copy(
            update={
                "clinician_decision": ClinicianDecision(
                    subject=principal.subject,
                    practitioner=principal.fhir_user or "",
                    decision="rejected",
                    at=now,
                    statement=command.reason,
                    policy=outcome,
                )
            }
        )
        await self._repo.update(rejected, expected_version=verification.version)
        self._metrics.decisions.labels("rejected", "false").inc()
        await self._record(principal.subject, "verification.reject", rejected, "success", {})
        return rejected

    # ------------------------------------------------------------- order checks

    async def validate_orders(
        self, command: ValidateOrders, principal: Principal
    ) -> OrderValidationResult:
        await self._access.require(
            principal,
            "orders:validate",
            resource_type="Patient",
            resource_id=command.patient_id or "*",
        )
        sigs = tuple(parse_sig(o.text, self._formulary) for o in command.orders)
        items = [
            OrderItem(
                kind=OrderKind.MEDICATION, description=sig.text, medication=sig, source="proposed"
            )
            for sig in sigs
        ]
        chart = await self._chart.chart(command.patient_id) if command.patient_id else None
        findings = tuple(self._validator.validate(items, chart=chart, allergies=command.allergies))
        for f in findings:
            self._metrics.findings.labels(f.rule, f.severity.value).inc()
        return OrderValidationResult(
            orders=sigs,
            findings=findings,
            chart_checked=chart is not None,
            safe_to_sign=not any(
                f.severity in {Severity.ERROR, Severity.CRITICAL} for f in findings
            ),
        )

    async def parse(self, text: str, principal: Principal) -> MedicationSig:
        await self._access.require(principal, "orders:validate", resource_type="sig")
        return parse_sig(text, self._formulary)

    # ------------------------------------------------------------------ helpers

    def _service_subject(self) -> Subject:
        return Subject(id=self._agent_id, kind="service")

    @staticmethod
    def _write_resource(verification_id: str, critical: int) -> Resource:
        return Resource(
            type="DocumentReference",
            id=verification_id,
            classification=DataClassification.RESTRICTED,
            attributes={"critical_findings": critical},
        )

    async def _record(
        self, actor: str, action: str, v: Verification, outcome: str, details: dict[str, Any]
    ) -> None:
        await self._audit.record(
            actor=actor,
            action=action,
            resource=f"verification:{v.id}",
            outcome=outcome,
            correlation_id=correlation_id(),
            details={"patient": f"Patient/{v.patient_id}", "status": v.status.value, **details},
        )


def _ungrounded(extraction: ClinicalExtraction) -> list[Finding]:
    items: list[tuple[str, str, tuple[int, ...], bool]] = [
        *(("problem", p.name, p.evidence, p.grounded) for p in extraction.problems),
        *(("medication", m.name, m.evidence, m.grounded) for m in extraction.medications),
        *(("order", o.description, o.evidence, o.grounded) for o in extraction.orders),
        *(("allergy", a.substance, a.evidence, a.grounded) for a in extraction.allergies),
    ]
    return [
        Finding(
            rule="ungrounded-item",
            severity=Severity.WARNING,
            message=f"{kind} '{name}' is not supported by the utterances it cites",
            item=name,
            evidence=evidence,
        )
        for kind, name, evidence, grounded in items
        if not grounded
    ]
