"""Order safety rules, consistency scoring and deterministic note rendering."""

from __future__ import annotations

import pytest

from clinical_documentation_verification.clinical.consistency import agreement, analyse
from clinical_documentation_verification.clinical.formulary import Formulary
from clinical_documentation_verification.clinical.note import render_note
from clinical_documentation_verification.clinical.orders import OrderValidator, stopped_medications
from clinical_documentation_verification.clinical.segmentation import segment
from clinical_documentation_verification.clinical.sig import parse_sig
from clinical_documentation_verification.domain.models import (
    AllergyMention,
    Band,
    ChartAllergy,
    ChartMedication,
    ChartSnapshot,
    ClinicalExtraction,
    DiffEntry,
    DiffKind,
    Finding,
    MedicationAction,
    MedicationMention,
    MentionStatus,
    OrderItem,
    OrderKind,
    ProblemMention,
    Severity,
    TranscriptInput,
)


def order(formulary: Formulary, text: str, **kw: object) -> OrderItem:
    return OrderItem(
        kind=OrderKind.MEDICATION,
        description=text,
        medication=parse_sig(text, formulary),
        evidence=(1,),
        **kw,  # type: ignore[arg-type]
    )


def rules(findings: list[Finding]) -> dict[str, Severity]:
    return {f.rule: f.severity for f in findings}


class TestOrderRules:
    @pytest.mark.parametrize(
        ("text", "rule", "severity"),
        [
            ("acetaminophen 1500 mg PO q6h", "dose-range-single", Severity.ERROR),
            ("acetaminophen 1000 mg PO q4h", "dose-range-daily", Severity.ERROR),
            ("acetaminophen 3 g PO q6h", "dose-range-single", Severity.CRITICAL),
            ("metoprolol 15 mg IV q6h", "dose-range-single", Severity.CRITICAL),
            ("metformin 500 mg IV BID", "route-incompatible", Severity.ERROR),
            ("ibuprofen 400 mg tablet IV q6h", "form-route-incompatible", Severity.ERROR),
            ("ibuprofen 400 mg q6h", "incomplete-order", Severity.ERROR),
            ("unobtainium 5 mg PO daily", "unknown-drug", Severity.WARNING),
            ("ibuprofen 5 mL PO q6h", "unit-not-comparable", Severity.WARNING),
            ("oxycodone 5 mg PO q6h", "controlled-substance", Severity.WARNING),
            ("warfarin 5.0 mg PO daily", "do-not-use-abbreviation", Severity.WARNING),
        ],
    )
    def test_single_order_rules(
        self, formulary: Formulary, text: str, rule: str, severity: Severity
    ) -> None:
        found = rules(OrderValidator(formulary).validate([order(formulary, text)]))
        assert found.get(rule) is severity

    def test_clean_order_has_no_findings(self, formulary: Formulary) -> None:
        assert (
            OrderValidator(formulary).validate([order(formulary, "lisinopril 20 mg PO daily")])
            == []
        )

    def test_non_medication_and_sigless_orders(self, formulary: Formulary) -> None:
        validator = OrderValidator(formulary)
        lab = OrderItem(kind=OrderKind.LAB, description="CBC")
        bare = OrderItem(kind=OrderKind.MEDICATION, description="something")
        assert rules(validator.validate([lab, bare])) == {"incomplete-order": Severity.ERROR}
        no_dose = order(formulary, "amoxicillin PO TID")
        assert "dose-range-single" not in rules(validator.validate([no_dose]))

    def test_cross_order_rules(self, formulary: Formulary) -> None:
        chart = ChartSnapshot(
            patient_reference="Patient/p",
            medications=(
                ChartMedication(
                    reference="MedicationRequest/1",
                    name="apixaban",
                    rxnorm="1364430",
                    status="active",
                ),
                ChartMedication(
                    reference="MedicationRequest/2", name="lisinopril", status="active"
                ),
                ChartMedication(
                    reference="MedicationRequest/3", name="sertraline", status="active"
                ),
                ChartMedication(
                    reference="MedicationRequest/4", name="unlisted herb", status="active"
                ),
                ChartMedication(
                    reference="MedicationRequest/5", name="atorvastatin", status="stopped"
                ),
                ChartMedication(
                    reference="MedicationRequest/6", name="simvastatin", status="active"
                ),
            ),
            allergies=(ChartAllergy(reference="AllergyIntolerance/1", substance="Penicillin"),),
        )
        orders = [
            order(formulary, "enoxaparin 40 mg SC daily"),
            order(formulary, "losartan 50 mg PO daily"),
            order(formulary, "ibuprofen 400 mg PO q6h"),
            order(formulary, "ibuprofen 600 mg PO q8h"),
            order(formulary, "lisinopril 10 mg PO daily", action=MedicationAction.START),
            order(formulary, "amoxicillin 500 mg PO TID"),
            order(formulary, "cephalexin 500 mg PO QID"),
            order(formulary, "aspirin 81 mg PO daily"),
            order(formulary, "atorvastatin 40 mg PO qhs"),
        ]
        findings = OrderValidator(formulary).validate(
            orders, chart=chart, allergies=["aspirin"], stopped=["simvastatin"]
        )
        found = rules(findings)
        assert found["therapeutic-duplication"] in {Severity.CRITICAL, Severity.WARNING}
        duplications = {f.item: f.severity for f in findings if f.rule == "therapeutic-duplication"}
        assert duplications["apixaban + enoxaparin"] is Severity.CRITICAL
        assert duplications["lisinopril + losartan"] is Severity.WARNING
        assert "atorvastatin + simvastatin" not in duplications  # simvastatin is being stopped
        interactions = {f.item for f in findings if f.rule == "drug-interaction"}
        assert {"apixaban + ibuprofen", "ibuprofen + sertraline"} <= interactions
        assert found["duplicate-order"] is Severity.ERROR
        assert found["already-active"] is Severity.WARNING
        allergy = {(f.item, f.rule) for f in findings if f.rule.startswith("allergy")}
        assert ("amoxicillin", "allergy-contraindication") in allergy
        assert ("cephalexin", "allergy-cross-reactivity") in allergy
        assert ("aspirin", "allergy-contraindication") in allergy
        assert ("ibuprofen", "allergy-cross-reactivity") in allergy

    def test_dose_change_is_not_already_active(self, formulary: Formulary) -> None:
        chart = ChartSnapshot(
            patient_reference="Patient/p",
            medications=(
                ChartMedication(
                    reference="MedicationRequest/1", name="lisinopril", status="active"
                ),
            ),
        )
        change = order(formulary, "lisinopril 20 mg PO daily", action=MedicationAction.CHANGE)
        assert OrderValidator(formulary).validate([change], chart=chart) == []

    def test_stopped_medications(self) -> None:
        meds = [
            MedicationMention(name="a", action=MedicationAction.STOP),
            MedicationMention(name="b", action=MedicationAction.CONTINUE),
        ]
        assert stopped_medications(meds) == ["a"]


class TestConsistency:
    def test_bands_and_components(self, formulary: Formulary) -> None:
        clean = ClinicalExtraction(
            source="deterministic",
            problems=(
                ProblemMention(name="Hypertension", status=MentionStatus.ACTIVE, evidence=(1,)),
            ),
        )
        report = analyse(clean, reference=None, diff=(), findings=(), formulary=formulary)
        assert report.band is Band.CONSISTENT
        assert report.score == 1.0
        assert "agreement" not in report.components

        ungrounded = clean.model_copy(
            update={
                "problems": (ProblemMention(name="x", status=MentionStatus.ACTIVE, grounded=False),)
            }
        )
        contradicted = (
            DiffEntry(category="problem", kind=DiffKind.CONTRADICTED, item="x", detail="d"),
            DiffEntry(category="medication", kind=DiffKind.NOT_DISCUSSED, item="y", detail="d"),
        )
        errors = [Finding(rule="incomplete-order", severity=Severity.ERROR, message="m")] * 2
        med_order = OrderItem(
            kind=OrderKind.MEDICATION,
            description="ibuprofen",
            medication=parse_sig("ibuprofen 400 mg", formulary),
        )
        weak = ungrounded.model_copy(update={"orders": (med_order,)})
        report = analyse(
            weak, reference=clean, diff=contradicted, findings=errors, formulary=formulary
        )
        assert report.band is Band.INCONSISTENT
        assert report.components["chart"] == 0.0
        assert report.components["completeness"] == 0.0
        assert any("not supported" in r for r in report.rationale)
        assert any("agree" in r for r in report.rationale)

        middling = analyse(
            clean,
            reference=None,
            diff=(
                DiffEntry(category="problem", kind=DiffKind.CONTRADICTED, item="x", detail="d"),
                DiffEntry(category="problem", kind=DiffKind.CONFIRMED, item="y", detail="d"),
            ),
            findings=[Finding(rule="dose-range-daily", severity=Severity.ERROR, message="m")],
            formulary=formulary,
        )
        assert middling.band is Band.NEEDS_REVIEW

        critical = analyse(
            clean,
            reference=None,
            diff=(),
            findings=[
                Finding(rule="allergy-contraindication", severity=Severity.CRITICAL, message="m")
            ],
            formulary=formulary,
        )
        assert critical.band is Band.INCONSISTENT
        assert critical.critical_findings == 1

    def test_agreement(self, formulary: Formulary) -> None:
        a = ClinicalExtraction(
            source="model",
            medications=(MedicationMention(name="Zestril", action=MedicationAction.CONTINUE),),
            allergies=(AllergyMention(substance="latex"),),
            problems=(ProblemMention(name="rare thing", status=MentionStatus.ACTIVE),),
        )
        b = ClinicalExtraction(
            source="deterministic",
            medications=(MedicationMention(name="lisinopril", action=MedicationAction.CONTINUE),),
        )
        assert agreement(a, b, formulary) == pytest.approx(0.5)
        empty = ClinicalExtraction(source="model")
        assert agreement(empty, empty, formulary) == 1.0


def test_note_rendering(formulary: Formulary) -> None:
    transcript = segment(TranscriptInput(text="Doctor: Hello.\nPatient: I feel fine."))
    extraction = ClinicalExtraction(
        source="deterministic",
        problems=(
            ProblemMention(
                name="Asthma", status=MentionStatus.HISTORICAL, evidence=(1,), grounded=False
            ),
        ),
        medications=(
            MedicationMention(name="albuterol", action=MedicationAction.CONTINUE, grounded=False),
        ),
        allergies=(AllergyMention(substance="latex", reaction="rash", evidence=(1,)),),
        orders=(
            OrderItem(
                kind=OrderKind.MEDICATION,
                description="albuterol 2 puffs",
                medication=parse_sig("albuterol 2 puffs", formulary),
                source="proposed",
            ),
            OrderItem(kind=OrderKind.LAB, description="CBC", evidence=(0,)),
        ),
    )
    findings = [
        Finding(rule="incomplete-order", severity=Severity.ERROR, message="order is missing route")
    ]
    report = analyse(extraction, reference=None, diff=(), findings=findings, formulary=formulary)
    note = render_note(
        note_title="Consult note",
        transcript=transcript,
        extraction=extraction,
        diff=(
            DiffEntry(
                category="allergy",
                kind=DiffKind.NEW,
                item="latex",
                detail="allergy not on the chart",
            ),
        ),
        findings=findings,
        consistency=report,
    )
    assert note.startswith("CONSULT NOTE")
    assert "Asthma (history of) UNVERIFIED [utt 1]" in note
    assert "latex (rash)" in note
    assert "(proposed order)" in note
    assert "ERROR incomplete-order" in note
    assert transcript.digest in note

    empty = render_note(
        note_title="Progress note",
        transcript=segment(TranscriptInput(text="Doctor: Hello.")),
        extraction=ClinicalExtraction(source="deterministic", no_known_allergies=True),
        diff=(),
        findings=(),
        consistency=report,
    )
    for line in (
        "No patient or caregiver statements captured.",
        "No known drug allergies stated.",
        "No orders placed.",
        "Encounter is consistent with the chart.",
    ):
        assert line in empty
    assert "Not discussed." in render_note(
        note_title="x",
        transcript=transcript,
        extraction=ClinicalExtraction(source="deterministic"),
        diff=(),
        findings=(),
        consistency=report,
    )
