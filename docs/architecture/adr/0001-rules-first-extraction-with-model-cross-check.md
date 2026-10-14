# ADR-0001: Rule-based extraction always runs; the model is cross-checked, not trusted

- Status: Accepted
- Date: 2026-10-14

## Context

Foundation models extract clinical facts from conversation far better than
rules on paraphrase and context, but they also produce plausible facts that
were never said, and their availability depends on a provider. A
verification service whose purpose is to catch documentation errors cannot
itself be the unverified source of them, and must keep working (and testing)
with no provider and no keys.

## Decision

- A deterministic extractor (formulary and clinical lexicons, NegEx-style
  assertion status, sig parser, action cues, speaker rules) runs on every
  transcript.
- When `CDV_PROVIDERS_FILE` is set, the model extracts through the agentic-core
  `ModelRouter` (Bedrock first). Output must validate against a strict JSON
  schema; invalid output is returned with the validation errors for a bounded
  number of corrective attempts.
- Model output is normalized by the same deterministic parsers (doses, routes,
  frequencies, codes are never taken from the model as numbers or codes),
  grounded against the cited utterances, and compared with the rule-based
  extraction. Agreement is a component of the consistency score.
- If the model path fails for any reason (guard block, provider outage,
  invalid output) the rule-based extraction is used and a finding records why.

## Consequences

- The service is useful with zero model spend and degrades gracefully.
- Unsupported model claims are visible to the clinician instead of silently
  entering the note.
- Two extractions cost CPU, not money; the rule path takes milliseconds.
- The lexicons need curation; coverage gaps lower agreement rather than
  hiding model errors.
