"""Consistency analysis: one explainable score for how far the draft documentation can be trusted.

    score = sum(weight_i * component_i) / sum(weight_i over available components)

Components, each in [0, 1]:

    grounding     share of extracted items supported by the utterances they cite      (0.25)
    agreement     F1 overlap of model and deterministic extraction (model runs only)  (0.15)
    chart         1 - contradicted / reconciled chart items                           (0.25)
    orders        1 - sum of order finding weights (info 0, warning .1, error .3,
                  critical 1), floored at 0                                           (0.20)
    completeness  share of medication orders with dose, route and frequency           (0.15)

Bands: any critical finding -> inconsistent; otherwise score >= 0.85 consistent,
score >= 0.60 needs review, else inconsistent. Every band still requires a
clinician signature before anything is written to the chart.
"""

from __future__ import annotations

from collections.abc import Sequence

from clinical_documentation_verification.clinical.formulary import Formulary
from clinical_documentation_verification.clinical.lexicon import PROBLEM_LEXICON
from clinical_documentation_verification.domain.models import (
    Band,
    ClinicalExtraction,
    ConsistencyReport,
    DiffEntry,
    DiffKind,
    Finding,
    OrderKind,
    Severity,
)

WEIGHTS = {
    "grounding": 0.25,
    "agreement": 0.15,
    "chart": 0.25,
    "orders": 0.20,
    "completeness": 0.15,
}
CONSISTENT_AT = 0.85
REVIEW_AT = 0.60
ORDER_RULES = frozenset(
    {
        "incomplete-order",
        "unknown-drug",
        "dose-range-single",
        "dose-range-daily",
        "unit-not-comparable",
        "route-incompatible",
        "form-route-incompatible",
        "duplicate-order",
        "already-active",
        "therapeutic-duplication",
        "drug-interaction",
        "allergy-contraindication",
        "allergy-cross-reactivity",
        "controlled-substance",
        "do-not-use-abbreviation",
    }
)


def extraction_keys(extraction: ClinicalExtraction, formulary: Formulary) -> set[str]:
    keys: set[str] = set()
    for med in extraction.medications:
        drug = formulary.resolve(med.name)
        keys.add(f"med:{drug.name if drug else med.name.lower()}:{med.action.value}")
    for problem in extraction.problems:
        concept = PROBLEM_LEXICON.by_name(problem.name)
        keys.add(
            f"problem:{concept.key if concept else problem.name.lower()}:{problem.status.value}"
        )
    for allergy in extraction.allergies:
        drug = formulary.resolve(allergy.substance)
        keys.add(f"allergy:{drug.name if drug else allergy.substance.lower()}")
    return keys


def agreement(a: ClinicalExtraction, b: ClinicalExtraction, formulary: Formulary) -> float:
    left, right = extraction_keys(a, formulary), extraction_keys(b, formulary)
    if not left and not right:
        return 1.0
    return 2 * len(left & right) / (len(left) + len(right))


def analyse(
    extraction: ClinicalExtraction,
    *,
    reference: ClinicalExtraction | None,
    diff: Sequence[DiffEntry],
    findings: Sequence[Finding],
    formulary: Formulary,
) -> ConsistencyReport:
    components: dict[str, float] = {}
    rationale: list[str] = []

    total = extraction.item_count()
    grounded = extraction.grounded_count()
    components["grounding"] = 1.0 if total == 0 else grounded / total
    if total and grounded < total:
        rationale.append(
            f"{total - grounded} of {total} extracted items are not supported by the transcript"
        )

    if reference is not None:
        components["agreement"] = agreement(extraction, reference, formulary)
        if components["agreement"] < 1.0:
            rationale.append(
                f"model and rule-based extraction agree on {components['agreement']:.0%} of items"
            )

    reconciled = [d for d in diff if d.kind is not DiffKind.NOT_DISCUSSED]
    contradicted = [d for d in reconciled if d.kind is DiffKind.CONTRADICTED]
    components["chart"] = 1.0 if not reconciled else 1 - len(contradicted) / len(reconciled)
    rationale.extend(f"chart contradiction: {d.item} ({d.detail})" for d in contradicted)

    order_findings = [f for f in findings if f.rule in ORDER_RULES]
    penalty = sum(f.severity.weight for f in order_findings)
    components["orders"] = max(0.0, 1.0 - penalty)
    serious = [f for f in order_findings if f.severity in {Severity.ERROR, Severity.CRITICAL}]
    rationale.extend(f"{f.severity.value} {f.rule}: {f.message}" for f in serious)

    med_orders = [o for o in extraction.orders if o.kind is OrderKind.MEDICATION]
    complete = [o for o in med_orders if o.medication is not None and o.medication.complete]
    components["completeness"] = 1.0 if not med_orders else len(complete) / len(med_orders)

    weight_sum = sum(WEIGHTS[name] for name in components)
    score = sum(WEIGHTS[name] * value for name, value in components.items()) / weight_sum
    score = round(min(1.0, max(0.0, score)), 4)
    critical = sum(1 for f in findings if f.severity is Severity.CRITICAL)
    if critical:
        band = Band.INCONSISTENT
        rationale.insert(0, f"{critical} critical finding(s) must be resolved or overridden")
    elif score >= CONSISTENT_AT:
        band = Band.CONSISTENT
    elif score >= REVIEW_AT:
        band = Band.NEEDS_REVIEW
    else:
        band = Band.INCONSISTENT
    return ConsistencyReport(
        score=score,
        band=band,
        components={k: round(v, 4) for k, v in components.items()},
        critical_findings=critical,
        rationale=tuple(rationale),
    )
