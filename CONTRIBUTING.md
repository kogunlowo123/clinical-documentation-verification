# Contributing

## Setup

```bash
uv sync
make check
```

## Requirements for every change

- `make check` passes: ruff, black, mypy `--strict`, bandit, contract drift,
  tests with at least 90% branch coverage.
- New behaviour has tests. Security controls need adversarial cases in
  `tests/security`; dependency failure modes need cases in `tests/chaos`.
- Clinical rule or reference-table changes (`src/clinical_documentation_verification/clinical/`,
  `clinical/data/formulary.yaml`, `policies/`) follow
  [docs/runbooks/reference-table-change.md](docs/runbooks/reference-table-change.md):
  a test that demonstrates the new behaviour and sign-off from clinical safety
  (pharmacy and therapeutics for dose limits).
- Public API changes regenerate contracts
  (`uv run python -m clinical_documentation_verification.contracts`) and are
  recorded in `CHANGELOG.md`.
- Architecturally significant decisions get an ADR in `docs/architecture/adr/`.
- No credentials, real patient data or PHI in code, tests, fixtures or issues.
  Use synthetic data only.

## Commit messages

Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`,
`chore:`). Breaking changes use `!` and a `BREAKING CHANGE:` footer.

## Review

Changes to `src/clinical_documentation_verification/security`,
`src/clinical_documentation_verification/clinical`, `policies/` or
`deployments/` need approval from a code owner (see `CODEOWNERS`).
