An ambient scribe can draft a visit note in seconds. The clinician still has to sign it, and the signature means "this is true."

The problem: a generated note can contain a dose nobody said, a diagnosis the patient denied, or an antibiotic ordered for someone with a documented allergy. Reviewing every line against the transcript and the chart takes the time the scribe was meant to save.

Why it matters: the signed note becomes the legal record that billing, care coordination and other agents build on.

The flow we built:
Transcript → Speaker segmentation → Extraction → FHIR chart review → Order checks → Consistency score → Clinician sign-off → DocumentReference + Provenance

How it works:
1. Split the transcript by speaker and section. Only clinicians can place orders.
2. Extract problems, medications, orders and allergies twice: rules always, Amazon Bedrock when configured. Model output must pass a JSON schema, then the same deterministic parsers, then grounding: every item must cite utterances that mention it.
3. Compare with the FHIR chart: new, confirmed, changed, contradicted, discontinued.
4. Screen orders: dose ceilings, route and form, duplicates, interactions, allergies, Joint Commission do-not-use abbreviations.
5. Score consistency with visible components. Any critical finding forces review.
6. Expose it over REST, MCP tools and A2A, with agent identity from our identity fabric.
7. Write nothing until a prescriber signs. Then one FHIR transaction: the note plus Provenance.

Best practices: treat spoken content as untrusted input; keep numbers out of the model's hands; make overrides explicit and audited; fail closed when evidence cannot be written.

Tradeoffs: two extraction paths cost CPU and maintenance. Rule lexicons miss phrasings. Dose tables need pharmacy ownership. Model latency adds seconds.

Value: clinicians review what needs a decision, not every sentence, and auditors can trace each line to its source.

As agents write more of the record, verification becomes the control point.

Question: should a documentation agent ever propose problem-list changes directly, or only surface them for a human?

github.com/kogunlowo123/clinical-documentation-verification

#AI #ArtificialIntelligence #GenerativeAI #AgenticAI #LLMOps #EnterpriseAI #AISecurity #RAG #MachineLearning #DataScience #CloudComputing #Kubernetes #MLOps #DigitalTransformation #Innovation #HealthcareAI #FHIR #PatientSafety
