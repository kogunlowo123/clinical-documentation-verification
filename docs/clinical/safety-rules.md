# Clinical safety rules

Rules are deterministic, versioned in Git and covered by tests in
`tests/unit/test_orders_and_consistency.py`. Findings carry a stable rule id,
a severity and the utterances they refer to.

## Order rules

| Rule id | Severity | Trigger |
|---|---|---|
| `incomplete-order` | error | Medication order without dose, route or frequency |
| `unknown-drug` | warning | Ingredient not in the reference table; dose not screened |
| `dose-range-single` | error; critical above 2x | Per-administration dose above the (route-specific) ceiling |
| `dose-range-daily` | error; critical above 2x | Maximum 24-hour amount implied by dose x frequency above the ceiling (PRN at the shortest interval, ranges at the upper bound) |
| `unit-not-comparable` | warning | Dose unit cannot be converted to the reference unit (e.g. mL against mg limits) |
| `route-incompatible` | error | Route not approved for the ingredient |
| `form-route-incompatible` | error | Dosage form cannot be given by that route (tablet IV) |
| `duplicate-order` | error | Same ingredient ordered twice in one encounter |
| `already-active` | warning | New start of a medication already active on the chart (dose changes excluded) |
| `therapeutic-duplication` | warning; critical for anticoagulants | Two agents of one duplication group across new orders and active chart medications, excluding medications being stopped |
| `drug-interaction` | critical (opioid + benzodiazepine); warning (NSAID + anticoagulant, SSRI + NSAID) | Class interaction |
| `allergy-contraindication` | critical | Ingredient or class matches a documented or stated allergy |
| `allergy-cross-reactivity` | warning | Cross-reactive class (penicillin and cephalosporin, NSAID and salicylate) |
| `controlled-substance` | warning | DEA scheduled drug; requires EPCS signing |
| `do-not-use-abbreviation` | warning | The Joint Commission "Do Not Use" list: U, IU, Q.D., Q.O.D., trailing zero, missing leading zero, MS/MSO4/MgSO4 |

## Documentation rules

| Rule id | Severity | Trigger |
|---|---|---|
| `ungrounded-item` | warning | Extracted item not supported by the utterances it cites |
| `prompt-injection-detected` | warning | Transcript content blocked by the content guard; model not called |
| `model-output-invalid`, `model-output-blocked`, `model-unavailable`, `model-extraction-fallback` | warning / info | Model path did not complete; rule-based extraction used |

## Chart reconciliation

| Encounter says | Chart says | Result |
|---|---|---|
| Problem active | Absent / active / resolved | new / confirmed / **contradicted** |
| Problem denied | Active | **contradicted** |
| Problem resolved | Active | changed (update problem list) |
| Medication stopped | Active / absent | discontinued / **contradicted** |
| Medication dose differs | Active | changed (shows both) |
| Medication not mentioned | Active | not discussed |
| "No known drug allergies" | Allergy recorded | **contradicted** |

## Consistency score

Weights: grounding 0.25, model/rule agreement 0.15 (model runs only), chart
0.25, orders 0.20, completeness 0.15, renormalized over the components
present. Bands: any critical finding makes the note inconsistent; otherwise
score ≥ 0.85 consistent, ≥ 0.60 needs review, below inconsistent. Signing an
inconsistent note or one with critical findings requires an override reason,
which is recorded in the audit chain and the clinician decision.

## Reference table governance

`src/clinical_documentation_verification/clinical/data/formulary.yaml` holds
usual adult maximum doses from US prescribing information, approved routes,
therapeutic classes, duplication groups, class interactions and allergy
classes. The limits are screening thresholds, not prescribing guidance, and
do not cover pediatric, renal or hepatic dosing. Pharmacy and therapeutics
owns the table; organizations with a commercial drug knowledge base load an
export in the same schema through `CDV_FORMULARY_FILE`. Changes follow
[the reference-table runbook](../runbooks/reference-table-change.md).
