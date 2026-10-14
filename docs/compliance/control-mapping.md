# Compliance control mapping

The evidence column names the artifact an auditor can inspect.

| Control objective | HIPAA Security Rule | HITRUST CSF | SOC 2 (TSC 2017) | ISO/IEC 27001:2022 | NIST SP 800-53 r5 | NIST AI RMF 1.0 | Evidence |
|---|---|---|---|---|---|---|---|
| Access limited to authorized users and agents | 164.312(a)(1) | 01.c, 01.v | CC6.1 | 5.15, 8.3 | AC-3, AC-6 | GOVERN 1.4 | `policies/documentation-verification.yaml`, `authorize:*` audit events |
| Unique identification and authentication | 164.312(a)(2)(i), 164.312(d) | 01.q | CC6.1 | 5.16, 5.17 | IA-2, IA-9 | MAP 3.5 | IdP and fabric verification (`security/principals.py`) |
| Minimum necessary | 164.502(b), 164.514(d) | 13.b | C1.1 | 5.12 | AC-6, PT-2 | MAP 4.2 | Read scopes limited to four FHIR resource types; identifier redaction before model |
| Integrity of ePHI | 164.312(c)(1) | 09.ab | PI1.2 | 8.24 | SI-7 | MEASURE 2.6 | Signature-gated write, Provenance, Attachment hash, optimistic locking |
| Audit controls | 164.312(b) | 09.aa | CC7.2 | 8.15 | AU-2, AU-9, AU-10 | MEASURE 2.8 | Signed hash chains in WORM storage, `verify_chain` |
| Transmission security | 164.312(e)(1) | 09.m | CC6.7 | 8.24 | SC-8 | — | TLS to FHIR, fabric, Bedrock; `rds.force_ssl` / `require_secure_transport` |
| Encryption at rest | 164.312(a)(2)(iv) | 06.d | CC6.1 | 8.24 | SC-28 | — | KMS-encrypted RDS, Secrets Manager, S3 (Terraform) |
| Business associate safeguards | 164.308(b)(1), 164.314(a) | 05.k | CC9.2 | 5.20 | SA-9 | GOVERN 6.1 | `configs/providers.yaml` lists BAA-covered Bedrock models only; classification ceiling |
| Human oversight of AI output | — | — | CC8.1 | 5.37 | — | GOVERN 3.2, MANAGE 2.4 | Clinician sign-off, override reason, override-rate alert |
| Validity and reliability of AI output | — | — | PI1.1 | — | SI-10 | MEASURE 2.5, MEASURE 2.9 | Schema validation, grounding, rule cross-check, consistency score |
| Transparency and explainability | — | — | — | — | — | MEASURE 2.9 | Findings with rule ids and citations; consistency rationale |
| Change management of clinical rules | 164.308(a)(8) | 10.k | CC8.1 | 8.32 | CM-3 | GOVERN 1.4 | Git history, CODEOWNERS, reference-table runbook, policy digest in every decision |
| Contingency and recovery | 164.308(a)(7) | 12.c | A1.2 | 5.29, 8.14 | CP-9, CP-10 | MANAGE 4.1 | Multi-AZ database, PITR, DR runbook |
| Incident response | 164.308(a)(6) | 11.a | CC7.3 | 5.24 | IR-4, SI-4 | MANAGE 4.3 | Alert rules, incident runbook |
| Vulnerability management | 164.308(a)(1)(ii)(B) | 10.m | CC7.1 | 8.8 | RA-5, SI-2 | — | CI: bandit, Semgrep, pip-audit, Trivy, Grype, Dependabot |

## Regulatory positioning

- **FDA clinical decision support.** The software presents findings with
  their basis (rule, cited utterance, chart resource) so a clinician can
  independently review them, and it does not acquire or analyze medical
  images or signals. It is designed to meet the non-device CDS criteria of
  section 520(o)(1)(E) of the FD&C Act as interpreted in FDA's 2022 CDS
  guidance. Each deploying organization should confirm the classification for
  its intended use.
- **21 CFR Part 11** does not apply to clinical documentation in general;
  where a deployment uses notes in regulated research, the signed audit chain,
  attributable signatures and Provenance support Part 11 record expectations.
- **ONC information blocking** is not affected: the service does not restrict
  access to EHR data.
