"""Response bodies for the HTTP API, MCP tools and A2A artifacts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from clinical_documentation_verification.domain.models import (
    ClinicalExtraction,
    ConsistencyReport,
    DiffEntry,
    FhirWriteResult,
    Finding,
    PolicyOutcome,
    SegmentedTranscript,
    Verification,
    VerificationStatus,
)


class DecisionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject: str
    practitioner: str
    decision: str
    at: datetime
    statement: str
    override_reason: str | None
    note_digest: str | None


class VerificationSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    patient_id: str
    encounter_id: str
    status: VerificationStatus
    band: str
    score: float
    critical_findings: int
    findings: int
    extraction_source: str
    document_reference: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, v: Verification) -> VerificationSummary:
        return cls(
            id=v.id,
            patient_id=v.patient_id,
            encounter_id=v.encounter_id,
            status=v.status,
            band=v.consistency.band.value,
            score=v.consistency.score,
            critical_findings=v.consistency.critical_findings,
            findings=len(v.findings),
            extraction_source=v.extraction.source,
            document_reference=v.write.document_reference if v.write else None,
            created_at=v.created_at,
            updated_at=v.updated_at,
        )


class VerificationView(BaseModel):
    """Everything a signing clinician needs to review, with citations into the transcript."""

    model_config = ConfigDict(frozen=True)

    id: str
    patient_id: str
    encounter_id: str
    note_type: str
    status: VerificationStatus
    submitted_by: str
    created_at: datetime
    updated_at: datetime
    consistency: ConsistencyReport
    findings: tuple[Finding, ...]
    chart_diff: tuple[DiffEntry, ...]
    extraction: ClinicalExtraction
    transcript: SegmentedTranscript
    draft_note: str
    write_policy: PolicyOutcome
    clinician_decision: DecisionView | None
    write: FhirWriteResult | None

    @classmethod
    def of(cls, v: Verification) -> VerificationView:
        decision = v.clinician_decision
        return cls(
            id=v.id,
            patient_id=v.patient_id,
            encounter_id=v.encounter_id,
            note_type=v.note_type,
            status=v.status,
            submitted_by=v.submitted_by,
            created_at=v.created_at,
            updated_at=v.updated_at,
            consistency=v.consistency,
            findings=v.findings,
            chart_diff=v.diff,
            extraction=v.extraction,
            transcript=v.transcript,
            draft_note=v.draft_note,
            write_policy=v.write_policy,
            clinician_decision=(
                DecisionView(
                    subject=decision.subject,
                    practitioner=decision.practitioner,
                    decision=decision.decision,
                    at=decision.at,
                    statement=decision.statement,
                    override_reason=decision.override_reason,
                    note_digest=decision.note_digest,
                )
                if decision
                else None
            ),
            write=v.write,
        )


class VerificationList(BaseModel):
    items: list[VerificationSummary]
    limit: int
    offset: int


class ErrorBody(BaseModel):
    error: str
    error_description: str
