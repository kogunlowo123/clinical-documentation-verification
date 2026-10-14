"""Render the draft note deterministically from verified, structured content.

The note is assembled from extracted and reconciled items, never generated
free-form by a model, so every statement in it traces to cited utterances
and every unresolved issue is visible to the signing clinician.
"""

from __future__ import annotations

from collections.abc import Sequence

from clinical_documentation_verification.clinical.reconciliation import describe
from clinical_documentation_verification.domain.models import (
    ClinicalExtraction,
    ConsistencyReport,
    DiffEntry,
    DiffKind,
    Finding,
    MentionStatus,
    OrderItem,
    OrderKind,
    SegmentedTranscript,
    Severity,
    SpeakerRole,
)

_STATUS_LABEL = {
    MentionStatus.ACTIVE: "active",
    MentionStatus.HISTORICAL: "history of",
    MentionStatus.RESOLVED: "resolved",
    MentionStatus.DENIED: "denies",
}


def _cite(evidence: tuple[int, ...]) -> str:
    return f" [utt {', '.join(str(i) for i in evidence)}]" if evidence else ""


def _section(title: str, body: list[str], empty: str) -> list[str]:
    return [title, *(body or [f"- {empty}"]), ""]


def _allergy_lines(extraction: ClinicalExtraction) -> list[str]:
    if extraction.allergies:
        return [
            f"- {a.substance}{f' ({a.reaction})' if a.reaction else ''}{_cite(a.evidence)}"
            for a in extraction.allergies
        ]
    if extraction.no_known_allergies:
        return ["- No known drug allergies stated."]
    return []


def _order_line(order: OrderItem) -> str:
    detail = describe(order.medication) if order.kind is OrderKind.MEDICATION else ""
    source = " (proposed order)" if order.source == "proposed" else ""
    return (
        f"- {order.kind.value}: {order.description}{f' [{detail}]' if detail else ''}"
        f"{source}{_cite(order.evidence)}"
    )


def _flag(grounded: bool) -> str:
    return "" if grounded else " UNVERIFIED"


def render_note(
    *,
    note_title: str,
    transcript: SegmentedTranscript,
    extraction: ClinicalExtraction,
    diff: Sequence[DiffEntry],
    findings: Sequence[Finding],
    consistency: ConsistencyReport,
) -> str:
    reported = [u for u in transcript.utterances if u.role is not SpeakerRole.CLINICIAN]
    serious = [f for f in findings if f.severity in {Severity.ERROR, Severity.CRITICAL}]
    lines: list[str] = [note_title.upper(), ""]
    lines += _section(
        "SUBJECTIVE",
        [
            f"- {u.role.value.capitalize()} reports: {u.text}{_cite((u.index,))}"
            for u in reported[:20]
        ],
        "No patient or caregiver statements captured.",
    )
    lines += _section(
        "PROBLEMS",
        [
            f"- {p.name} ({_STATUS_LABEL[p.status]}){_flag(p.grounded)}{_cite(p.evidence)}"
            for p in extraction.problems
        ],
        "None discussed.",
    )
    lines += _section(
        "MEDICATIONS",
        [
            f"- {m.name}: {m.action.value}, {describe(m.sig)}{_flag(m.grounded)}{_cite(m.evidence)}"
            for m in extraction.medications
        ],
        "None discussed.",
    )
    lines += _section("ALLERGIES", _allergy_lines(extraction), "Not discussed.")
    lines += _section(
        "PLAN AND ORDERS", [_order_line(o) for o in extraction.orders], "No orders placed."
    )
    lines += _section(
        "CHART RECONCILIATION",
        [
            f"- {d.category} {d.item}: {d.kind.value}; {d.detail}"
            for d in diff
            if d.kind is not DiffKind.CONFIRMED
        ],
        "Encounter is consistent with the chart.",
    )
    lines.append("VERIFICATION")
    lines.append(f"- Consistency score {consistency.score:.2f} ({consistency.band.value}).")
    lines.extend(f"- {f.severity.value.upper()} {f.rule}: {f.message}" for f in serious)
    lines.append(f"- Source transcript {transcript.digest}.")
    return "\n".join(lines) + "\n"
