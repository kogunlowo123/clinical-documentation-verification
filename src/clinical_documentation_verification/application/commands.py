"""Inputs and outputs of the application use cases (shared by REST, MCP and A2A)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from clinical_documentation_verification.domain.models import (
    Finding,
    MedicationSig,
    TranscriptInput,
)

FhirId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9\-.]{1,64}$")]
NoteType = Literal["11506-3", "34117-2", "11488-4", "18842-5", "34111-5"]


class ProposedOrder(BaseModel):
    """A draft medication order from the ordering system, in sig form."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(
        min_length=3, max_length=500, examples=["amoxicillin 500 mg PO TID x 10 days"]
    )


class SubmitVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    patient_id: FhirId
    encounter_id: FhirId
    note_type: NoteType | None = Field(default=None, description="LOINC document type of the note")
    transcript: TranscriptInput
    proposed_orders: tuple[ProposedOrder, ...] = Field(default=(), max_length=100)


class ApproveVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    statement: str = Field(
        min_length=10,
        max_length=2000,
        description="Clinician attestation, e.g. 'I have reviewed and verified this note.'",
    )
    override_reason: str | None = Field(
        default=None,
        min_length=10,
        max_length=1000,
        description="Required to sign a note with critical findings or an inconsistent band",
    )
    amended_note: str | None = Field(
        default=None, min_length=20, max_length=100_000, description="Clinician-edited note text"
    )


class RejectVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str = Field(min_length=10, max_length=2000)


class ValidateOrders(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    patient_id: FhirId | None = Field(
        default=None, description="When set, chart allergies and active medications are checked"
    )
    orders: tuple[ProposedOrder, ...] = Field(min_length=1, max_length=100)
    allergies: tuple[Annotated[str, StringConstraints(min_length=2, max_length=100)], ...] = Field(
        default=(), max_length=50
    )


class OrderValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    orders: tuple[MedicationSig, ...]
    findings: tuple[Finding, ...]
    chart_checked: bool
    safe_to_sign: bool = Field(description="True when no error or critical finding was raised")


class ParseSig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=3, max_length=500)
