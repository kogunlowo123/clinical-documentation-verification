"""Segmentation, assertion status, deterministic extraction, grounding, reconciliation."""

from __future__ import annotations

import pytest

from clinical_documentation_verification.clinical.extraction import (
    DeterministicExtractor,
    normalize_substance,
)
from clinical_documentation_verification.clinical.formulary import Formulary
from clinical_documentation_verification.clinical.grounding import Grounder
from clinical_documentation_verification.clinical.lexicon import (
    LAB_LEXICON,
    PROBLEM_LEXICON,
    find_referrals,
)
from clinical_documentation_verification.clinical.negation import assertion_status
from clinical_documentation_verification.clinical.reconciliation import ChartReconciler, describe
from clinical_documentation_verification.clinical.segmentation import parse_text, segment
from clinical_documentation_verification.clinical.sig import parse_sig
from clinical_documentation_verification.domain.models import (
    AllergyMention,
    ChartAllergy,
    ChartCondition,
    ChartMedication,
    ChartSnapshot,
    ClinicalExtraction,
    Coding,
    DiffKind,
    MedicationAction,
    MedicationMention,
    MentionStatus,
    OrderItem,
    OrderKind,
    ProblemMention,
    RawUtterance,
    Section,
    SpeakerRole,
    TranscriptInput,
)
from tests.conftest import CLEAN_ENCOUNTER, ENCOUNTER


def seg(text: str, **roles: SpeakerRole) -> object:
    return segment(TranscriptInput(text=text, speaker_roles=dict(roles)))


class TestSegmentation:
    def test_labels_timestamps_continuations_and_sections(self) -> None:
        t = segment(TranscriptInput(text=ENCOUNTER))
        assert [u.role for u in t.utterances][:2] == [SpeakerRole.CLINICIAN, SpeakerRole.PATIENT]
        assert t.utterances[0].start_s == 5.0
        assert "atorvastatin" in t.utterances[3].text  # continuation line joined to the turn
        sections = {u.section for u in t.utterances}
        assert {
            Section.SUBJECTIVE,
            Section.MEDICATIONS,
            Section.OBJECTIVE,
            Section.ASSESSMENT_PLAN,
        } <= sections
        assert t.digest.startswith("sha256:")
        assert t.text_of((0,)).startswith("Good morning")

    def test_each_labelled_turn_is_an_utterance(self) -> None:
        t = segment(TranscriptInput(text="Dr. Lee: One.\nDr. Lee: Two.\nPatient: Three."))
        assert [u.text for u in t.utterances] == ["One.", "Two.", "Three."]

    def test_non_speaker_colons_are_content(self) -> None:
        t = segment(
            TranscriptInput(text="Doctor: Vitals today.\nBlood pressure: 140/90\nPatient: ok")
        )
        assert t.utterances[0].text == "Vitals today. Blood pressure: 140/90"

    def test_unattributed_preamble(self) -> None:
        t = segment(TranscriptInput(text="Recording started.\nNurse: Hello."))
        assert t.utterances[0].speaker == "Unattributed"
        assert t.utterances[0].role is SpeakerRole.OTHER

    def test_anonymous_speakers_are_inferred(self) -> None:
        text = (
            "Speaker 1: What brings you in? Any allergies?\n"
            "Speaker 2: I've been coughing and my chest hurts.\n"
            "Speaker 1: I'm going to order a chest x-ray.\n"
            "Speaker 3: Should I wait outside?"
        )
        t = segment(TranscriptInput(text=text))
        assert t.speakers == {
            "Speaker 1": SpeakerRole.CLINICIAN,
            "Speaker 2": SpeakerRole.PATIENT,
            "Speaker 3": SpeakerRole.OTHER,
        }

    def test_declared_roles_and_titled_names(self) -> None:
        t = segment(
            TranscriptInput(
                text="Mrs. Alvarez: My son is here.\nJordan: Hi.\nMother: He has asthma.",
                speaker_roles={
                    "Jordan": SpeakerRole.PATIENT,
                    "Mrs. Alvarez": SpeakerRole.CAREGIVER,
                },
            )
        )
        assert t.speakers["Jordan"] is SpeakerRole.PATIENT
        assert t.speakers["Mrs. Alvarez"] is SpeakerRole.CAREGIVER
        assert t.speakers["Mother"] is SpeakerRole.CAREGIVER

    def test_preseparated_utterances_and_interpreter(self) -> None:
        t = segment(
            TranscriptInput(
                utterances=(
                    RawUtterance(speaker="Clinician", text="How have you been?", start_s=1.5),
                    RawUtterance(speaker="Interpreter", text="She says better."),
                )
            )
        )
        assert [u.role for u in t.utterances] == [SpeakerRole.CLINICIAN, SpeakerRole.INTERPRETER]
        assert parse_text("\n\n", {}) == []

    def test_unknown_speakers_without_cues(self) -> None:
        t = segment(TranscriptInput(text="Speaker A: hello\nSpeaker B: hello"))
        assert set(t.speakers.values()) == {SpeakerRole.PATIENT, SpeakerRole.OTHER}

    def test_input_validation(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            TranscriptInput()
        with pytest.raises(ValueError, match="exactly one"):
            TranscriptInput(text="x", utterances=(RawUtterance(speaker="a", text="b"),))


@pytest.mark.parametrize(
    ("sentence", "concept", "status"),
    [
        ("She has diabetes and hypertension.", "hypertension", MentionStatus.ACTIVE),
        ("No history of diabetes.", "diabetes", MentionStatus.DENIED),
        ("He denies chest pain.", "chest pain", MentionStatus.DENIED),
        ("Pneumonia was ruled out.", "pneumonia", MentionStatus.DENIED),
        ("History of asthma as a child.", "asthma", MentionStatus.HISTORICAL),
        ("Her pneumonia has resolved.", "pneumonia", MentionStatus.RESOLVED),
        ("He no longer has migraines.", "migraines", MentionStatus.RESOLVED),
        ("No fever but she has asthma.", "asthma", MentionStatus.ACTIVE),
        ("Denies nausea. Has back pain.", "back pain", MentionStatus.ACTIVE),
    ],
)
def test_assertion_status(sentence: str, concept: str, status: MentionStatus) -> None:
    start = sentence.lower().index(concept)
    assert assertion_status(sentence, start, start + len(concept)) is status


def test_lexicons() -> None:
    assert PROBLEM_LEXICON.by_name("HTN") is PROBLEM_LEXICON.by_name("hypertension")
    assert PROBLEM_LEXICON.by_name("poorly controlled type 2 diabetes") is not None
    assert PROBLEM_LEXICON.by_name("gout") is None
    assert (
        PROBLEM_LEXICON.by_codes([Coding(system="http://hl7.org/fhir/sid/icd-10-cm", code="I10")])
        is not None
    )
    assert PROBLEM_LEXICON.by_codes([Coding(system="x", code="y")]) is None
    assert LAB_LEXICON.by_name("a1c") is not None
    assert find_referrals("I'll refer you to cardiology and see nephrology")[0][0] == "cardiology"


class TestDeterministicExtraction:
    def test_encounter(self, formulary: Formulary) -> None:
        e = DeterministicExtractor(formulary).extract(segment(TranscriptInput(text=ENCOUNTER)))
        meds = {m.name: m for m in e.medications}
        assert meds["lisinopril"].action is MedicationAction.CHANGE
        assert meds["lisinopril"].sig is not None
        assert meds["lisinopril"].sig.dose.value == 20
        assert meds["atorvastatin"].action is MedicationAction.STOP
        assert meds["amoxicillin"].action is MedicationAction.START
        assert meds["aspirin"].action is MedicationAction.REPORTED
        kinds = {(o.kind, o.description) for o in e.orders}
        assert (OrderKind.LAB, "Hemoglobin A1c") in kinds
        assert (OrderKind.LAB, "Basic metabolic panel") in kinds
        assert (OrderKind.REFERRAL, "Referral to cardiology") in kinds
        ordered = {o.medication.drug for o in e.orders if o.medication is not None}
        assert ordered == {"lisinopril", "amoxicillin"}
        assert e.allergies == (
            AllergyMention(substance="penicillin", reaction="hives", evidence=(4,)),
        )
        assert [p.name for p in e.problems] == ["Hypertension"]
        assert e.source == "deterministic"
        assert e.grounded_count() == e.item_count()

    def test_patients_cannot_order_and_clinicians_override(self, formulary: Formulary) -> None:
        text = (
            "Patient: I started taking ibuprofen 400 mg. I want to start tramadol.\n"
            "Doctor: Stop the ibuprofen. Let's start aspirin 81 mg PO daily.\n"
            "Patient: I stopped aspirin last month.\n"
            "Doctor: Avoid naproxen. Restart aspirin 81 mg daily. Continue aspirin.\n"
        )
        e = DeterministicExtractor(formulary).extract(segment(TranscriptInput(text=text)))
        meds = {m.name: m for m in e.medications}
        assert meds["ibuprofen"].action is MedicationAction.STOP
        assert meds["tramadol"].action is MedicationAction.REPORTED
        assert meds["aspirin"].action is MedicationAction.START
        assert "naproxen" not in meds
        assert {o.medication.drug for o in e.orders if o.medication} == {"aspirin"}  # type: ignore[union-attr]

    def test_problems_allergies_and_imaging(self, formulary: Formulary) -> None:
        text = (
            "Patient: No known drug allergies. I don't have diabetes. My back pain is better.\n"
            "Doctor: History of asthma noted. Your back pain is from osteoarthritis.\n"
            "Doctor: Let's get a chest x-ray and an EKG. We will not order an MRI."
        )
        e = DeterministicExtractor(formulary).extract(segment(TranscriptInput(text=text)))
        status = {p.name: p.status for p in e.problems}
        assert status["Type 2 diabetes mellitus"] is MentionStatus.DENIED
        assert status["Asthma"] is MentionStatus.HISTORICAL
        assert status["Low back pain"] is MentionStatus.ACTIVE
        assert e.no_known_allergies is True
        imaging = {o.description for o in e.orders if o.kind is OrderKind.IMAGING}
        assert imaging == {"Chest X-ray", "Electrocardiogram"}

    def test_allergy_forms(self, formulary: Formulary) -> None:
        text = (
            "Patient: I have a sulfa allergy and I'm allergic to Advil "
            "and allergic to the class of drugs."
        )
        e = DeterministicExtractor(formulary).extract(segment(TranscriptInput(text=text)))
        assert {a.substance for a in e.allergies} == {"sulfa", "ibuprofen"}
        assert normalize_substance("the", formulary) == ""
        assert normalize_substance("latex", formulary) == "latex"


class TestGrounding:
    def test_marks_unsupported_items(self, formulary: Formulary) -> None:
        t = segment(TranscriptInput(text=CLEAN_ENCOUNTER))
        extraction = ClinicalExtraction(
            source="model",
            problems=(
                ProblemMention(name="Hypertension", status=MentionStatus.ACTIVE, evidence=(3,)),
                ProblemMention(name="Heart failure", status=MentionStatus.ACTIVE, evidence=(3,)),
            ),
            medications=(
                MedicationMention(
                    name="lisinopril", action=MedicationAction.CONTINUE, evidence=(1,)
                ),
                MedicationMention(name="warfarin", action=MedicationAction.CONTINUE, evidence=(1,)),
                MedicationMention(name="aspirin", action=MedicationAction.REPORTED, evidence=(99,)),
            ),
            orders=(
                OrderItem(kind=OrderKind.LAB, description="Lipid panel", evidence=(5,)),
                OrderItem(kind=OrderKind.IMAGING, description="MRI", evidence=(5,)),
                OrderItem(
                    kind=OrderKind.REFERRAL, description="Referral to cardiology", evidence=(5,)
                ),
                OrderItem(
                    kind=OrderKind.MEDICATION,
                    description="atorvastatin 40 mg",
                    medication=parse_sig("atorvastatin 40 mg", formulary),
                    evidence=(4,),
                ),
                OrderItem(kind=OrderKind.MEDICATION, description="x", source="proposed"),
            ),
            allergies=(AllergyMention(substance="penicillin", evidence=(2,)),),
        )
        g = Grounder(formulary).ground(extraction, t)
        assert [p.grounded for p in g.problems] == [True, False]
        assert [m.grounded for m in g.medications] == [True, False, False]
        assert [o.grounded for o in g.orders] == [True, False, False, True, True]
        assert g.allergies[0].grounded is True

    def test_multi_word_descriptions(self, formulary: Formulary) -> None:
        t = segment(TranscriptInput(text="Doctor: We will draw a panel for your lipid levels."))
        extraction = ClinicalExtraction(
            source="model",
            orders=(OrderItem(kind=OrderKind.LAB, description="lipid panel", evidence=(0,)),),
            problems=(ProblemMention(name="of the", status=MentionStatus.ACTIVE, evidence=(0,)),),
        )
        g = Grounder(formulary).ground(extraction, t)
        assert g.orders[0].grounded is True
        assert g.problems[0].grounded is False


def chart(*, meds: tuple[ChartMedication, ...] = (), **kw: object) -> ChartSnapshot:
    return ChartSnapshot(patient_reference="Patient/p", medications=meds, **kw)  # type: ignore[arg-type]


class TestReconciliation:
    def test_problem_matrix(self, formulary: Formulary) -> None:
        conditions = (
            ChartCondition(
                reference="Condition/1",
                name="HTN",
                coding=(Coding(system="http://snomed.info/sct", code="38341003"),),
                clinical_status="active",
            ),
            ChartCondition(reference="Condition/2", name="Asthma", clinical_status="resolved"),
            ChartCondition(reference="Condition/3", name="GERD", clinical_status="active"),
            ChartCondition(reference="Condition/4", name="Rare syndrome", clinical_status="active"),
        )
        problems = (
            ProblemMention(name="Hypertension", status=MentionStatus.ACTIVE),
            ProblemMention(name="Asthma", status=MentionStatus.ACTIVE),
            ProblemMention(name="Gastroesophageal reflux disease", status=MentionStatus.DENIED),
            ProblemMention(name="Migraine", status=MentionStatus.ACTIVE),
            ProblemMention(name="Migraine headache", status=MentionStatus.DENIED),
            ProblemMention(name="GERD", status=MentionStatus.RESOLVED),
            ProblemMention(name="asthma", status=MentionStatus.RESOLVED),
            ProblemMention(name="pneumonia", status=MentionStatus.RESOLVED),
            ProblemMention(name="DVT", status=MentionStatus.HISTORICAL),
            ProblemMention(name="Rare syndrome", status=MentionStatus.HISTORICAL),
        )
        diff = ChartReconciler(formulary).diff(
            ClinicalExtraction(source="deterministic", problems=problems),
            chart(conditions=conditions),
        )
        kinds = [(d.item, d.kind) for d in diff]
        assert kinds == [
            ("Hypertension", DiffKind.CONFIRMED),
            ("Asthma", DiffKind.CONTRADICTED),
            ("Gastroesophageal reflux disease", DiffKind.CONTRADICTED),
            ("Migraine", DiffKind.NEW),
            ("GERD", DiffKind.CHANGED),
            ("asthma", DiffKind.CONFIRMED),
            ("DVT", DiffKind.NEW),
            ("Rare syndrome", DiffKind.CONFIRMED),
        ]

    def test_medication_matrix(self, formulary: Formulary) -> None:
        lisinopril = ChartMedication(
            reference="MedicationRequest/1",
            name="lisinopril 10 mg",
            rxnorm="29046",
            sig=parse_sig("lisinopril 10 mg daily", formulary),
            status="active",
        )
        metformin = ChartMedication(
            reference="MedicationRequest/2",
            name="Glucophage",
            sig=parse_sig("metformin 500 mg BID", formulary),
            status="active",
        )
        aspirin = ChartMedication(
            reference="MedicationRequest/3", name="aspirin", sig=None, status="active"
        )
        statin = ChartMedication(
            reference="MedicationRequest/4", name="atorvastatin", status="on-hold"
        )
        old = ChartMedication(reference="MedicationRequest/5", name="warfarin", status="stopped")
        meds = (
            MedicationMention(
                name="lisinopril",
                action=MedicationAction.CONTINUE,
                sig=parse_sig("lisinopril 10 mg daily", formulary),
            ),
            MedicationMention(
                name="metformin",
                action=MedicationAction.REPORTED,
                sig=parse_sig("metformin 500 mg TID", formulary),
            ),
            MedicationMention(name="aspirin", action=MedicationAction.CHANGE),
            MedicationMention(name="atorvastatin", action=MedicationAction.STOP),
            MedicationMention(name="warfarin", action=MedicationAction.STOP),
            MedicationMention(name="gabapentin", action=MedicationAction.CHANGE),
            MedicationMention(name="ibuprofen", action=MedicationAction.REPORTED),
            MedicationMention(
                name="amoxicillin",
                action=MedicationAction.START,
                sig=parse_sig("amoxicillin 500 mg PO TID", formulary),
            ),
        )
        diff = ChartReconciler(formulary).diff(
            ClinicalExtraction(source="deterministic", medications=meds),
            chart(
                meds=(
                    lisinopril,
                    metformin,
                    aspirin,
                    statin,
                    old,
                    ChartMedication(
                        reference="MedicationRequest/6",
                        name="omeprazole",
                        sig=parse_sig("omeprazole 20 mg daily", formulary),
                        status="active",
                    ),
                )
            ),
        )
        kinds = {(d.item, d.kind) for d in diff}
        assert kinds == {
            ("lisinopril", DiffKind.CONFIRMED),
            ("metformin", DiffKind.CHANGED),
            ("aspirin", DiffKind.CHANGED),
            ("atorvastatin", DiffKind.DISCONTINUED),
            ("warfarin", DiffKind.CONTRADICTED),
            ("gabapentin", DiffKind.CONTRADICTED),
            ("ibuprofen", DiffKind.NEW),
            ("amoxicillin", DiffKind.NEW),
            ("omeprazole", DiffKind.NOT_DISCUSSED),
        }
        assert describe(None) == "no dose stated"
        assert (
            describe(parse_sig("acetaminophen 500 mg PO q6h prn pain", formulary))
            == "500 mg PO Q6H PRN"
        )

    def test_dose_change_detected_by_strength(self, formulary: Formulary) -> None:
        charted = ChartMedication(
            reference="MedicationRequest/1",
            name="metformin",
            sig=parse_sig("metformin 500 mg BID", formulary),
            status="active",
        )
        said = MedicationMention(
            name="metformin",
            action=MedicationAction.CONTINUE,
            sig=parse_sig("metformin 1000 mg BID", formulary),
        )
        [entry] = ChartReconciler(formulary).diff(
            ClinicalExtraction(source="deterministic", medications=(said,)), chart(meds=(charted,))
        )
        assert entry.kind is DiffKind.CHANGED
        assert "1000 mg" in entry.detail

    def test_allergies(self, formulary: Formulary) -> None:
        allergies = (
            ChartAllergy(reference="AllergyIntolerance/1", substance="Penicillin"),
            ChartAllergy(reference="AllergyIntolerance/2", substance="Zestril"),
        )
        mentioned = ClinicalExtraction(
            source="deterministic",
            allergies=(
                AllergyMention(substance="penicillin"),
                AllergyMention(substance="latex"),
                AllergyMention(substance="lisinopril"),
            ),
        )
        diff = ChartReconciler(formulary).diff(mentioned, chart(allergies=allergies))
        assert {(d.item, d.kind) for d in diff} == {
            ("penicillin", DiffKind.CONFIRMED),
            ("latex", DiffKind.NEW),
            ("lisinopril", DiffKind.CONFIRMED),
        }
        nkda = ClinicalExtraction(source="deterministic", no_known_allergies=True)
        contradictions = ChartReconciler(formulary).diff(nkda, chart(allergies=allergies))
        assert {d.kind for d in contradictions} == {DiffKind.CONTRADICTED}
