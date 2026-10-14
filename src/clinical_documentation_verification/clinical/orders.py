"""Order safety validation.

Rules, each producing findings with a stable rule id:

- ``incomplete-order``           medication order without dose, route or frequency
- ``unknown-drug``               ingredient not in the reference table; limits not checked
- ``dose-range-single``          per-administration dose above the ceiling (critical above 2x)
- ``dose-range-daily``           24 h amount above the ceiling (critical above 2x)
- ``unit-not-comparable``        dose unit cannot be converted to the reference unit
- ``route-incompatible``         route not approved for the drug
- ``form-route-incompatible``    dosage form cannot be given by that route
- ``duplicate-order``            same ingredient ordered more than once
- ``already-active``             new start of a medication already active on the chart
- ``therapeutic-duplication``    two agents from one duplication group (orders + active chart)
- ``drug-interaction``           class interaction (e.g. opioid with benzodiazepine)
- ``allergy-contraindication``   order conflicts with a documented allergy
- ``allergy-cross-reactivity``   order cross-reacts with a documented allergy
- ``controlled-substance``       scheduled drug; needs prescriber controlled-substance signing
- ``do-not-use-abbreviation``    The Joint Commission "Do Not Use" abbreviation in the order text
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from itertools import combinations

from clinical_documentation_verification.clinical.formulary import Drug, Formulary
from clinical_documentation_verification.clinical.reconciliation import (
    ACTIVE_MEDICATION_STATUSES,
)
from clinical_documentation_verification.clinical.sig import FORM_ROUTES, daily_amount, dose_in
from clinical_documentation_verification.domain.models import (
    ChartSnapshot,
    Finding,
    MedicationAction,
    MedicationMention,
    OrderItem,
    OrderKind,
    Severity,
)


def _over(value: float, ceiling: float) -> Severity:
    return Severity.CRITICAL if value > 2 * ceiling else Severity.ERROR


class OrderValidator:
    def __init__(self, formulary: Formulary) -> None:
        self._formulary = formulary

    def validate(
        self,
        orders: Sequence[OrderItem],
        *,
        chart: ChartSnapshot | None = None,
        allergies: Iterable[str] = (),
        stopped: Iterable[str] = (),
    ) -> list[Finding]:
        findings: list[Finding] = []
        ordered: list[tuple[OrderItem, Drug]] = []
        for order in orders:
            if order.kind is not OrderKind.MEDICATION:
                continue
            drug, item_findings = self._single(order)
            findings.extend(item_findings)
            if drug is not None:
                ordered.append((order, drug))

        findings.extend(self._duplicates(ordered))
        stopped_names = {self._name(s) for s in stopped}
        active = self._active_chart_drugs(chart, exclude=stopped_names)
        findings.extend(self._already_active(ordered, active))
        findings.extend(self._class_rules(ordered, active))
        substances = {a.strip().lower() for a in allergies if a.strip()}
        if chart is not None:
            substances |= {a.substance.strip().lower() for a in chart.allergies}
        findings.extend(self._allergy_rules(ordered, substances))
        return findings

    # --------------------------------------------------------------- per-order rules

    def _single(self, order: OrderItem) -> tuple[Drug | None, list[Finding]]:
        sig = order.medication
        label = order.description
        findings: list[Finding] = []
        if sig is None:
            return None, [
                Finding(
                    rule="incomplete-order",
                    severity=Severity.ERROR,
                    message="medication order has no dose, route or frequency",
                    item=label,
                    evidence=order.evidence,
                )
            ]
        missing = [
            name
            for name, value in (
                ("dose", sig.dose),
                ("route", sig.route),
                ("frequency", sig.frequency),
            )
            if value is None
        ]
        if missing:
            findings.append(
                Finding(
                    rule="incomplete-order",
                    severity=Severity.ERROR,
                    message=f"order is missing {', '.join(missing)}",
                    item=label,
                    evidence=order.evidence,
                )
            )
        findings.extend(
            Finding(
                rule="do-not-use-abbreviation",
                severity=Severity.WARNING,
                message=f"order text uses a Joint Commission 'Do Not Use' abbreviation: {flag}",
                item=label,
                evidence=order.evidence,
            )
            for flag in sig.quality_flags
        )
        drug = (self._formulary.get(sig.rxnorm) if sig.rxnorm else None) or (
            self._formulary.resolve(sig.drug or label)
        )
        if drug is None:
            findings.append(
                Finding(
                    rule="unknown-drug",
                    severity=Severity.WARNING,
                    message="ingredient not in the medication reference table; dose not screened",
                    item=label,
                    evidence=order.evidence,
                )
            )
            return None, findings
        label = drug.name
        if drug.schedule is not None:
            findings.append(
                Finding(
                    rule="controlled-substance",
                    severity=Severity.WARNING,
                    message=f"Schedule {drug.schedule[1:]} controlled substance; requires "
                    "prescriber controlled-substance signing (EPCS)",
                    item=label,
                    evidence=order.evidence,
                )
            )
        if sig.route is not None and sig.route not in drug.routes:
            findings.append(
                Finding(
                    rule="route-incompatible",
                    severity=Severity.ERROR,
                    message=f"{drug.name} is not given {sig.route.value}; approved routes: "
                    f"{', '.join(sorted(r.value for r in drug.routes))}",
                    item=label,
                    evidence=order.evidence,
                )
            )
        if sig.form is not None and sig.route is not None:
            allowed = FORM_ROUTES.get(sig.form, frozenset())
            if sig.route not in allowed:
                findings.append(
                    Finding(
                        rule="form-route-incompatible",
                        severity=Severity.ERROR,
                        message=f"{sig.form} cannot be given {sig.route.value}",
                        item=label,
                        evidence=order.evidence,
                    )
                )
        findings.extend(self._dose(order, drug))
        return drug, findings

    def _dose(self, order: OrderItem, drug: Drug) -> list[Finding]:
        sig = order.medication
        if sig is None or sig.dose is None:
            return []
        single = dose_in(sig, drug.unit)
        if single is None:
            return [
                Finding(
                    rule="unit-not-comparable",
                    severity=Severity.WARNING,
                    message=f"dose in {sig.dose.unit} cannot be compared with limits in "
                    f"{drug.unit}",
                    item=drug.name,
                    evidence=order.evidence,
                )
            ]
        limits = drug.limits(sig.route)
        findings: list[Finding] = []
        if limits.max_single_dose is not None and single > limits.max_single_dose:
            findings.append(
                Finding(
                    rule="dose-range-single",
                    severity=_over(single, limits.max_single_dose),
                    message=f"{single:g} {drug.unit} per dose exceeds the "
                    f"{limits.max_single_dose:g} "
                    f"{drug.unit} ceiling",
                    item=drug.name,
                    evidence=order.evidence,
                )
            )
        daily = daily_amount(sig, drug.unit)
        if (
            limits.max_daily_dose is not None
            and daily is not None
            and daily > limits.max_daily_dose
        ):
            findings.append(
                Finding(
                    rule="dose-range-daily",
                    severity=_over(daily, limits.max_daily_dose),
                    message=f"{daily:g} {drug.unit} per 24 h exceeds the {limits.max_daily_dose:g} "
                    f"{drug.unit} ceiling",
                    item=drug.name,
                    evidence=order.evidence,
                )
            )
        return findings

    # ------------------------------------------------------------ cross-order rules

    def _name(self, value: str) -> str:
        drug = self._formulary.resolve(value)
        return drug.name if drug else value.strip().lower()

    def _active_chart_drugs(self, chart: ChartSnapshot | None, *, exclude: set[str]) -> list[Drug]:
        if chart is None:
            return []
        drugs: list[Drug] = []
        for med in chart.medications:
            if med.status not in ACTIVE_MEDICATION_STATUSES:
                continue
            drug = (
                self._formulary.get(med.rxnorm) if med.rxnorm else None
            ) or self._formulary.resolve(med.name)
            if drug is not None and drug.name not in exclude:
                drugs.append(drug)
        return drugs

    @staticmethod
    def _duplicates(ordered: list[tuple[OrderItem, Drug]]) -> list[Finding]:
        seen: dict[str, OrderItem] = {}
        findings: list[Finding] = []
        for order, drug in ordered:
            if drug.name in seen:
                findings.append(
                    Finding(
                        rule="duplicate-order",
                        severity=Severity.ERROR,
                        message=f"{drug.name} is ordered more than once",
                        item=drug.name,
                        evidence=tuple(dict.fromkeys((*seen[drug.name].evidence, *order.evidence))),
                    )
                )
            else:
                seen[drug.name] = order
        return findings

    @staticmethod
    def _already_active(ordered: list[tuple[OrderItem, Drug]], active: list[Drug]) -> list[Finding]:
        names = {d.name for d in active}
        return [
            Finding(
                rule="already-active",
                severity=Severity.WARNING,
                message=f"{drug.name} is already active on the chart; confirm this replaces it",
                item=drug.name,
                evidence=order.evidence,
            )
            for order, drug in ordered
            if drug.name in names and order.action is not MedicationAction.CHANGE
        ]

    def _class_rules(
        self, ordered: list[tuple[OrderItem, Drug]], active: list[Drug]
    ) -> list[Finding]:
        new = {drug.name: drug for _, drug in ordered}
        pool = dict(new)
        for drug in active:
            pool.setdefault(drug.name, drug)
        evidence = {drug.name: order.evidence for order, drug in ordered}
        findings: list[Finding] = []
        reported: set[tuple[str, str, str]] = set()
        for first, second in combinations(sorted(pool), 2):
            if first not in new and second not in new:
                continue  # both pre-existing: outside the scope of this encounter's orders
            a, b = pool[first], pool[second]
            for group_id, group in self._formulary.duplication_groups.items():
                if a.classes & group.classes and b.classes & group.classes:
                    key = (group_id, first, second)
                    if key not in reported:
                        reported.add(key)
                        findings.append(
                            Finding(
                                rule="therapeutic-duplication",
                                severity=group.severity,
                                message=f"{first} and {second} are both {group_id} therapy",
                                item=f"{first} + {second}",
                                evidence=(*evidence.get(first, ()), *evidence.get(second, ())),
                            )
                        )
            for interaction in self._formulary.interactions:
                x, y = interaction.classes
                if (x in a.classes and y in b.classes) or (y in a.classes and x in b.classes):
                    findings.append(
                        Finding(
                            rule="drug-interaction",
                            severity=interaction.severity,
                            message=f"{interaction.message}: {first} + {second}",
                            item=f"{first} + {second}",
                            evidence=(*evidence.get(first, ()), *evidence.get(second, ())),
                        )
                    )
        return findings

    def _allergy_rules(
        self, ordered: list[tuple[OrderItem, Drug]], substances: set[str]
    ) -> list[Finding]:
        findings: list[Finding] = []
        for substance in sorted(substances):
            rule = self._formulary.allergy_rule(substance)
            allergen = self._formulary.resolve(substance)
            for order, drug in ordered:
                if (allergen is not None and allergen.name == drug.name) or (
                    drug.classes & rule.contraindicated
                ):
                    findings.append(
                        Finding(
                            rule="allergy-contraindication",
                            severity=Severity.CRITICAL,
                            message=f"{drug.name} ordered for a patient allergic to {substance}",
                            item=drug.name,
                            evidence=order.evidence,
                        )
                    )
                elif drug.classes & rule.cross_reactive:
                    findings.append(
                        Finding(
                            rule="allergy-cross-reactivity",
                            severity=Severity.WARNING,
                            message=f"{drug.name} may cross-react with documented "
                            f"{substance} allergy",
                            item=drug.name,
                            evidence=order.evidence,
                        )
                    )
        return findings


def stopped_medications(medications: Iterable[MedicationMention]) -> list[str]:
    return [m.name for m in medications if m.action is MedicationAction.STOP]
