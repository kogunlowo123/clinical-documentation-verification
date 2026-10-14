# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [0.1.0] - 2026-10-14

### Added

- Transcript segmentation by speaker and encounter section, with role inference for
  anonymous diarization labels.
- Rule-based clinical extraction (problems with NegEx-style assertion status,
  medications with action cues, lab/imaging/referral orders, allergies) and
  Amazon Bedrock extraction through the agentic-core model router with JSON-schema
  validation, corrective retries, content guard and rule-based cross-check.
- Medication sig parser (dose, unit conversion, route, form, frequency, PRN) with
  Joint Commission "Do Not Use" abbreviation detection.
- Grounding of every extracted item in the utterances it cites.
- FHIR R4 chart review (Condition, MedicationRequest, AllergyIntolerance) with paging
  restricted to the configured origin and SMART backend-services authentication.
- Order safety rules: dose ceilings, route/form compatibility, duplicates, therapeutic
  duplication, class interactions, allergy contraindication and cross-reactivity,
  controlled substances, completeness.
- Consistency score and bands; deterministic draft note with citations.
- Clinician sign-off with policy enforcement (agentic-core PolicyEngine, identity-fabric
  authorization) and idempotent DocumentReference + Provenance transaction.
- REST API, MCP server (streamable HTTP) and A2A Agent Card with JSON-RPC.
- Signed per-replica audit chains, Prometheus metrics, OpenTelemetry traces.
- Helm chart, Kubernetes namespace and secret sync, Terraform for AWS, Azure and GCP.
- CI: lint, types, SAST, contract drift, tests (unit, security, chaos, performance,
  e2e, PostgreSQL integration), Helm/kubeconform, Terraform validate, Trivy, Syft,
  Grype, Semgrep, container smoke test; release with GHCR image, OCI chart and SBOM.
