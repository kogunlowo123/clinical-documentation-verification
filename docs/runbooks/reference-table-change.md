# Runbook: changing clinical rules or the medication reference table

Applies to `clinical/data/formulary.yaml`, rule code in `clinical/`, the
consistency weights and `policies/documentation-verification.yaml`.

1. Open a change with the clinical rationale and source (prescribing
   information section, institutional guideline, or committee decision).
2. Add or update a test that shows the behaviour change with synthetic data
   (`tests/unit/test_orders_and_consistency.py`, `test_sig_and_formulary.py`).
3. Approval: pharmacy and therapeutics for dose limits, routes and
   interactions; clinical safety for policy and score changes; a code owner for
   the code.
4. `make check`; CI must be green.
5. Release with a CHANGELOG entry. The new policy digest appears in every
   decision after deployment; dose-table version in `formulary.version`.
6. For organizations using a commercial drug knowledge base, export it in the
   same schema, validate with `Formulary.from_file` in a test, and set
   `CDV_FORMULARY_FILE`.
