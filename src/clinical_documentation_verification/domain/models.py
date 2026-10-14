"""Value objects and the verification aggregate.

Everything here is immutable (frozen Pydantic models) so a verification can
be hashed, persisted and replayed exactly. Clinical vocabularies follow FHIR
R4 and HL7 code systems where one exists.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from clinical_documentation_verification.domain.errors import InvalidStateError

FROZEN = ConfigDict(frozen=True)


# --------------------------------------------------------------------- transcript


class SpeakerRole(StrEnum):
    CLINICIAN = "clinician"
    PATIENT = "patient"
    CAREGIVER = "caregiver"
    INTERPRETER = "interpreter"
    OTHER = "other"


class Section(StrEnum):
    SUBJECTIVE = "subjective"
    MEDICATIONS = "medications"
    OBJECTIVE = "objective"
    ASSESSMENT_PLAN = "assessment_plan"


class RawUtterance(BaseModel):
    """One diarized turn as delivered by an ambient-capture or ASR system."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    speaker: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=10_000)
    start_s: float | None = Field(default=None, ge=0)


class TranscriptInput(BaseModel):
    """Either a labelled plain-text transcript or pre-diarized utterances."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str | None = Field(default=None, min_length=1, max_length=200_000)
    utterances: tuple[RawUtterance, ...] | None = Field(default=None, min_length=1, max_length=5000)
    speaker_roles: dict[str, SpeakerRole] = Field(
        default_factory=dict,
        description="Optional mapping of speaker label to role, e.g. {'Speaker 1': 'clinician'}",
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> TranscriptInput:
        if (self.text is None) == (self.utterances is None):
            raise ValueError("provide exactly one of text or utterances")
        return self


class Utterance(BaseModel):
    model_config = FROZEN

    index: int = Field(ge=0)
    speaker: str
    role: SpeakerRole
    section: Section
    text: str
    start_s: float | None = None


class SegmentedTranscript(BaseModel):
    model_config = FROZEN

    utterances: tuple[Utterance, ...]
    speakers: dict[str, SpeakerRole]
    digest: str = Field(description="sha256 of the canonical transcript text")

    def text_of(self, indices: tuple[int, ...]) -> str:
        wanted = set(indices)
        return " ".join(u.text for u in self.utterances if u.index in wanted)


# ------------------------------------------------------------------- medications


class Route(StrEnum):
    PO = "PO"
    IV = "IV"
    IM = "IM"
    SC = "SC"
    SL = "SL"
    PR = "PR"
    INH = "INH"
    TOP = "TOP"
    TD = "TD"
    OPH = "OPH"
    OTIC = "OTIC"
    NASAL = "NASAL"
    NG = "NG"


class Dose(BaseModel):
    model_config = FROZEN

    value: float = Field(gt=0, description="Upper bound when a range was given")
    unit: str
    min_value: float | None = Field(default=None, gt=0)


class Frequency(BaseModel):
    model_config = FROZEN

    code: str = Field(description="Normalized code, e.g. DAILY, BID, Q6H, Q4-6H, ONCE")
    doses_per_day: float | None = Field(default=None, gt=0)
    interval_hours: float | None = Field(default=None, gt=0)
    one_time: bool = False


class MedicationSig(BaseModel):
    """A medication instruction parsed into structured, checkable parts."""

    model_config = FROZEN

    text: str
    drug: str | None = None
    rxnorm: str | None = None
    dose: Dose | None = None
    route: Route | None = None
    form: str | None = None
    frequency: Frequency | None = None
    prn: bool = False
    indication: str | None = None
    quality_flags: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.dose is not None and self.route is not None and self.frequency is not None


# ---------------------------------------------------------------------- extraction


class Coding(BaseModel):
    model_config = FROZEN

    system: str
    code: str
    display: str | None = None


class MentionStatus(StrEnum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    DENIED = "denied"
    HISTORICAL = "historical"


class MedicationAction(StrEnum):
    START = "start"
    CONTINUE = "continue"
    CHANGE = "change"
    STOP = "stop"
    REPORTED = "reported"


class OrderKind(StrEnum):
    MEDICATION = "medication"
    LAB = "lab"
    IMAGING = "imaging"
    REFERRAL = "referral"
    PROCEDURE = "procedure"


class ProblemMention(BaseModel):
    model_config = FROZEN

    name: str
    coding: tuple[Coding, ...] = ()
    status: MentionStatus
    evidence: tuple[int, ...] = ()
    grounded: bool = True


class MedicationMention(BaseModel):
    model_config = FROZEN

    name: str
    rxnorm: str | None = None
    sig: MedicationSig | None = None
    action: MedicationAction
    evidence: tuple[int, ...] = ()
    grounded: bool = True


class OrderItem(BaseModel):
    model_config = FROZEN

    kind: OrderKind
    description: str
    coding: tuple[Coding, ...] = ()
    medication: MedicationSig | None = None
    action: MedicationAction | None = None
    evidence: tuple[int, ...] = ()
    grounded: bool = True
    source: Literal["transcript", "proposed"] = "transcript"


class AllergyMention(BaseModel):
    model_config = FROZEN

    substance: str
    reaction: str | None = None
    evidence: tuple[int, ...] = ()
    grounded: bool = True


class ClinicalExtraction(BaseModel):
    model_config = FROZEN

    problems: tuple[ProblemMention, ...] = ()
    medications: tuple[MedicationMention, ...] = ()
    orders: tuple[OrderItem, ...] = ()
    allergies: tuple[AllergyMention, ...] = ()
    no_known_allergies: bool = False
    source: Literal["model", "deterministic"]
    model: str | None = None
    attempts: int = Field(default=1, ge=0)

    def item_count(self) -> int:
        return len(self.problems) + len(self.medications) + len(self.orders) + len(self.allergies)

    def grounded_count(self) -> int:
        items: list[ProblemMention | MedicationMention | OrderItem | AllergyMention] = [
            *self.problems,
            *self.medications,
            *self.orders,
            *self.allergies,
        ]
        return sum(1 for item in items if item.grounded)


# ------------------------------------------------------------------------- chart


class ChartCondition(BaseModel):
    model_config = FROZEN

    reference: str
    name: str
    coding: tuple[Coding, ...] = ()
    clinical_status: str


class ChartMedication(BaseModel):
    model_config = FROZEN

    reference: str
    name: str
    rxnorm: str | None = None
    sig: MedicationSig | None = None
    status: str


class ChartAllergy(BaseModel):
    model_config = FROZEN

    reference: str
    substance: str
    rxnorm: str | None = None
    criticality: str | None = None


class ChartSnapshot(BaseModel):
    model_config = FROZEN

    patient_reference: str
    conditions: tuple[ChartCondition, ...] = ()
    medications: tuple[ChartMedication, ...] = ()
    allergies: tuple[ChartAllergy, ...] = ()
    versions: tuple[str, ...] = Field(
        default=(), description="Versioned references of every resource read, for provenance"
    )


class DiffKind(StrEnum):
    NEW = "new"
    CONFIRMED = "confirmed"
    CHANGED = "changed"
    CONTRADICTED = "contradicted"
    DISCONTINUED = "discontinued"
    NOT_DISCUSSED = "not_discussed"


class DiffEntry(BaseModel):
    model_config = FROZEN

    category: Literal["problem", "medication", "allergy"]
    kind: DiffKind
    item: str
    chart_reference: str | None = None
    evidence: tuple[int, ...] = ()
    detail: str


# ---------------------------------------------------------------------- findings


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def weight(self) -> float:
        return {"info": 0.0, "warning": 0.1, "error": 0.3, "critical": 1.0}[self.value]


class Finding(BaseModel):
    model_config = FROZEN

    rule: str
    severity: Severity
    message: str
    item: str | None = None
    evidence: tuple[int, ...] = ()


class Band(StrEnum):
    CONSISTENT = "consistent"
    NEEDS_REVIEW = "needs_review"
    INCONSISTENT = "inconsistent"


class ConsistencyReport(BaseModel):
    model_config = FROZEN

    score: float = Field(ge=0, le=1)
    band: Band
    components: dict[str, float]
    critical_findings: int = Field(ge=0)
    rationale: tuple[str, ...]


# ------------------------------------------------------------------ verification


class VerificationStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    WRITTEN = "written"
    REJECTED = "rejected"


_TRANSITIONS: dict[VerificationStatus, frozenset[VerificationStatus]] = {
    VerificationStatus.PENDING_APPROVAL: frozenset(
        {VerificationStatus.APPROVED, VerificationStatus.REJECTED}
    ),
    VerificationStatus.APPROVED: frozenset({VerificationStatus.WRITTEN}),
    VerificationStatus.WRITTEN: frozenset(),
    VerificationStatus.REJECTED: frozenset(),
}


class PolicyOutcome(BaseModel):
    model_config = FROZEN

    action: str
    decision: str
    matched_rules: tuple[str, ...]
    policy_version: str
    policy_digest: str
    reason: str
    fabric_decision: str | None = None


class ClinicianDecision(BaseModel):
    model_config = FROZEN

    subject: str
    practitioner: str = Field(description="FHIR reference of the clinician, e.g. Practitioner/123")
    decision: Literal["approved", "rejected"]
    at: datetime
    statement: str
    override_reason: str | None = None
    final_note: str | None = Field(default=None, description="Signed note text (approvals only)")
    note_digest: str | None = None
    policy: PolicyOutcome


class FhirWriteResult(BaseModel):
    model_config = FROZEN

    document_reference: str
    provenance: str
    written_at: datetime


class Verification(BaseModel):
    """Aggregate root: one transcript verified against one patient's chart."""

    model_config = FROZEN

    id: str
    patient_id: str
    encounter_id: str
    note_type: str
    status: VerificationStatus
    submitted_by: str
    created_at: datetime
    updated_at: datetime
    transcript: SegmentedTranscript
    extraction: ClinicalExtraction
    reference_extraction: ClinicalExtraction | None = Field(
        default=None, description="Deterministic extraction kept for cross-checking a model run"
    )
    chart: ChartSnapshot
    diff: tuple[DiffEntry, ...]
    findings: tuple[Finding, ...]
    consistency: ConsistencyReport
    draft_note: str
    write_policy: PolicyOutcome
    clinician_decision: ClinicianDecision | None = None
    write: FhirWriteResult | None = None
    idempotency_key: str | None = None
    version: int = Field(default=1, ge=1)

    def transition(self, target: VerificationStatus, *, at: datetime) -> Verification:
        if target not in _TRANSITIONS[self.status]:
            raise InvalidStateError(
                f"verification {self.id} is {self.status}; cannot become {target}"
            )
        return self.model_copy(
            update={"status": target, "updated_at": at, "version": self.version + 1}
        )
