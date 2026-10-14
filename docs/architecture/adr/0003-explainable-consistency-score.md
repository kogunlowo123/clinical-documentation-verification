# ADR-0003: A weighted, explainable consistency score with hard safety overrides

- Status: Accepted
- Date: 2026-10-14

## Context

Clinicians need one signal for how much review a draft needs, but an opaque
model-generated confidence cannot be audited or tuned, and a score must never
let a dangerous order look acceptable because everything else is clean.

## Decision

- The score is a weighted mean of five interpretable components (grounding,
  model/rule agreement, chart contradictions, order findings, order
  completeness), each in [0, 1], with weights in code and documented in
  `docs/clinical/safety-rules.md`.
- Bands are fixed thresholds (0.85 consistent, 0.60 needs review). Any
  critical finding forces the inconsistent band regardless of the score.
- Each report carries the component values and a rationale list so the
  clinician and auditors see why.

## Consequences

- Changes to weights or thresholds are code changes with tests and review.
- The score is comparable across model versions, which supports monitoring of
  model drift through the agreement component.
- Simple weights will not capture every clinical nuance; the band is a triage
  aid, and every band still requires a signature.
