"""Chart review: reconcile what was said in the encounter with what the FHIR chart holds.

Problems are matched by code (SNOMED CT / ICD-10-CM through the lexicon) and
then by name; medications by RxNorm ingredient and then by name; allergies by
normalized substance. Each extracted item is classified as new, confirmed,
changed, contradicted or discontinued against the chart; active chart
medications nobody mentioned are reported as not discussed so the clinician
can complete medication reconciliation.
"""

from __future__ import annotations

from clinical_documentation_verification.clinical.formulary import Formulary
from clinical_documentation_verification.clinical.lexicon import PROBLEM_LEXICON
from clinical_documentation_verification.clinical.sig import daily_amount, dose_in
from clinical_documentation_verification.domain.models import (
    ChartAllergy,
    ChartCondition,
    ChartMedication,
    ChartSnapshot,
    ClinicalExtraction,
    DiffEntry,
    DiffKind,
    MedicationAction,
    MedicationSig,
    MentionStatus,
)

ACTIVE_CONDITION_STATUSES = frozenset({"active", "recurrence", "relapse"})
ACTIVE_MEDICATION_STATUSES = frozenset({"active", "on-hold"})

# (mention status, chart state) -> (diff kind, detail template) or None when not reportable.
_PROBLEM_RULES: dict[tuple[MentionStatus, str], tuple[DiffKind, str] | None] = {
    (MentionStatus.ACTIVE, "absent"): (
        DiffKind.NEW,
        "discussed as a current problem; not on the problem list",
    ),
    (MentionStatus.ACTIVE, "active"): (DiffKind.CONFIRMED, "matches an active problem"),
    (MentionStatus.ACTIVE, "inactive"): (
        DiffKind.CONTRADICTED,
        "discussed as current; charted as {status}",
    ),
    (MentionStatus.DENIED, "absent"): None,
    (MentionStatus.DENIED, "active"): (
        DiffKind.CONTRADICTED,
        "denied in the encounter; charted as active",
    ),
    (MentionStatus.DENIED, "inactive"): None,
    (MentionStatus.RESOLVED, "absent"): None,
    (MentionStatus.RESOLVED, "active"): (DiffKind.CHANGED, "reported resolved; charted as active"),
    (MentionStatus.RESOLVED, "inactive"): (DiffKind.CONFIRMED, "resolved in chart and encounter"),
    (MentionStatus.HISTORICAL, "absent"): (
        DiffKind.NEW,
        "history mentioned; not on the problem list",
    ),
    (MentionStatus.HISTORICAL, "active"): (DiffKind.CONFIRMED, "history matches the chart"),
    (MentionStatus.HISTORICAL, "inactive"): (DiffKind.CONFIRMED, "history matches the chart"),
}
_UNCHARTED: dict[MedicationAction, tuple[DiffKind, str]] = {
    MedicationAction.CHANGE: (
        DiffKind.CONTRADICTED,
        "dose change for a medication not active on the chart",
    ),
    MedicationAction.START: (DiffKind.NEW, "new medication ({sig})"),
    MedicationAction.REPORTED: (
        DiffKind.NEW,
        "reported in the encounter ({sig}); not on the chart",
    ),
}


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return True
    return abs(a - b) <= max(1e-6, 0.001 * max(abs(a), abs(b)))


def describe(sig: MedicationSig | None) -> str:
    if sig is None:
        return "no dose stated"
    parts = []
    if sig.dose is not None:
        parts.append(f"{sig.dose.value:g} {sig.dose.unit}")
    if sig.route is not None:
        parts.append(sig.route.value)
    if sig.frequency is not None:
        parts.append(sig.frequency.code)
    if sig.prn:
        parts.append("PRN")
    return " ".join(parts) or "no dose stated"


class ChartReconciler:
    def __init__(self, formulary: Formulary) -> None:
        self._formulary = formulary

    # ---------------------------------------------------------------- matching

    def _condition_key(self, condition: ChartCondition) -> str:
        concept = PROBLEM_LEXICON.by_codes(condition.coding) or PROBLEM_LEXICON.by_name(
            condition.name
        )
        return concept.key if concept else condition.name.strip().lower()

    def _problem_key(self, name: str) -> str:
        concept = PROBLEM_LEXICON.by_name(name)
        return concept.key if concept else name.strip().lower()

    def _drug_key(self, name: str, rxnorm: str | None) -> str:
        drug = (self._formulary.get(rxnorm) if rxnorm else None) or self._formulary.resolve(name)
        return drug.name if drug else name.strip().lower()

    def _allergy_key(self, substance: str) -> str:
        key = substance.strip().lower()
        if key in self._formulary.allergy_classes:
            return key
        drug = self._formulary.resolve(key)
        return drug.name if drug else key

    # -------------------------------------------------------------------- diff

    def diff(self, extraction: ClinicalExtraction, chart: ChartSnapshot) -> tuple[DiffEntry, ...]:
        return (
            *self._problems(extraction, chart),
            *self._medications(extraction, chart),
            *self._allergies(extraction, chart),
        )

    def _problems(self, extraction: ClinicalExtraction, chart: ChartSnapshot) -> list[DiffEntry]:
        charted = {self._condition_key(c): c for c in chart.conditions}
        entries: list[DiffEntry] = []
        for problem in extraction.problems:
            match = charted.get(self._problem_key(problem.name))
            if match is None:
                state = "absent"
            elif match.clinical_status in ACTIVE_CONDITION_STATUSES:
                state = "active"
            else:
                state = "inactive"
            rule = _PROBLEM_RULES[(problem.status, state)]
            if rule is None:
                continue
            kind, template = rule
            entries.append(
                DiffEntry(
                    category="problem",
                    kind=kind,
                    item=problem.name,
                    chart_reference=match.reference if match else None,
                    evidence=problem.evidence,
                    detail=template.format(status=match.clinical_status if match else ""),
                )
            )
        return entries

    def _medications(self, extraction: ClinicalExtraction, chart: ChartSnapshot) -> list[DiffEntry]:
        active = {
            self._drug_key(m.name, m.rxnorm): m
            for m in chart.medications
            if m.status in ACTIVE_MEDICATION_STATUSES
        }
        mentioned: set[str] = set()
        entries: list[DiffEntry] = []
        for med in extraction.medications:
            key = self._drug_key(med.name, med.rxnorm)
            mentioned.add(key)
            charted = active.get(key)
            kind, detail = self._medication_kind(med.action, med.sig, charted)
            entries.append(
                DiffEntry(
                    category="medication",
                    kind=kind,
                    item=med.name,
                    chart_reference=charted.reference if charted else None,
                    evidence=med.evidence,
                    detail=detail,
                )
            )
        entries.extend(
            DiffEntry(
                category="medication",
                kind=DiffKind.NOT_DISCUSSED,
                item=charted.name,
                chart_reference=charted.reference,
                detail=f"active on the chart ({describe(charted.sig)}); not addressed in the "
                "encounter",
            )
            for key, charted in active.items()
            if key not in mentioned
        )
        return entries

    def _medication_kind(
        self, action: MedicationAction, sig: MedicationSig | None, charted: ChartMedication | None
    ) -> tuple[DiffKind, str]:
        if action is MedicationAction.STOP:
            if charted is None:
                return DiffKind.CONTRADICTED, "stopped in the encounter but not active on the chart"
            return DiffKind.DISCONTINUED, "stopped in the encounter; chart order still active"
        if charted is None:
            kind, template = _UNCHARTED.get(action, _UNCHARTED[MedicationAction.REPORTED])
            return kind, template.format(sig=describe(sig))
        if self._differs(sig, charted.sig):
            return DiffKind.CHANGED, f"encounter: {describe(sig)}; chart: {describe(charted.sig)}"
        if action is MedicationAction.CHANGE:
            return DiffKind.CHANGED, f"dose change discussed; chart: {describe(charted.sig)}"
        return DiffKind.CONFIRMED, f"matches the chart ({describe(charted.sig)})"

    def _differs(self, said: MedicationSig | None, charted: MedicationSig | None) -> bool:
        if said is None or charted is None or said.dose is None or charted.dose is None:
            return False
        drug = self._formulary.resolve(said.drug or "") if said.drug else None
        unit = drug.unit if drug else said.dose.unit
        if not _same(dose_in(said, unit), dose_in(charted, unit)):
            return True
        return not _same(daily_amount(said, unit), daily_amount(charted, unit))

    def _allergies(self, extraction: ClinicalExtraction, chart: ChartSnapshot) -> list[DiffEntry]:
        charted: dict[str, ChartAllergy] = {
            self._allergy_key(a.substance): a for a in chart.allergies
        }
        entries = [
            DiffEntry(
                category="allergy",
                kind=DiffKind.CONFIRMED if key in charted else DiffKind.NEW,
                item=allergy.substance,
                chart_reference=charted[key].reference if key in charted else None,
                evidence=allergy.evidence,
                detail="documented allergy" if key in charted else "allergy not on the chart",
            )
            for allergy in extraction.allergies
            for key in (self._allergy_key(allergy.substance),)
        ]
        if extraction.no_known_allergies and chart.allergies:
            entries.extend(
                DiffEntry(
                    category="allergy",
                    kind=DiffKind.CONTRADICTED,
                    item=a.substance,
                    chart_reference=a.reference,
                    detail="no known allergies stated in the encounter; allergy on the chart",
                )
                for a in chart.allergies
            )
        return entries
