"""Clinical extraction agent: foundation-model extraction with schema validation and retry.

The transcript is untrusted content (anyone in the room can say anything),
so it is sent with ``TrustLevel.EXTERNAL`` and passes the agentic-core
``ContentGuard`` (prompt-injection scoring, credential blocking, identifier
redaction, spotlighting) before any provider sees it. The model must return
JSON matching ``ModelExtraction``; invalid output is sent back with the
validation errors for a bounded number of corrective attempts. Model output
is normalized through the same deterministic sig parser and lexicons as the
rule-based extractor, then grounded against the transcript.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from agentic_core import ProviderUnavailableError
from agentic_core.domain.classification import DataClassification, TrustLevel
from agentic_core.models import Message, ModelRequest, Role
from agentic_core.models.router import ModelRouter
from agentic_core.security import ContentGuard
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from clinical_documentation_verification.clinical.extraction import normalize_substance
from clinical_documentation_verification.clinical.formulary import Formulary
from clinical_documentation_verification.clinical.lexicon import (
    IMAGING_LEXICON,
    LAB_LEXICON,
    PROBLEM_LEXICON,
)
from clinical_documentation_verification.clinical.sig import parse_sig
from clinical_documentation_verification.domain.models import (
    AllergyMention,
    ClinicalExtraction,
    Finding,
    MedicationAction,
    MedicationMention,
    MentionStatus,
    OrderItem,
    OrderKind,
    ProblemMention,
    SegmentedTranscript,
    Severity,
)

Evidence = list[int]


class ModelProblem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    status: MentionStatus
    evidence: Evidence = Field(min_length=1, max_length=50)


class ModelMedication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    sig_text: str | None = Field(default=None, max_length=500)
    action: MedicationAction
    evidence: Evidence = Field(min_length=1, max_length=50)


class ModelOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: OrderKind
    description: str = Field(min_length=1, max_length=300)
    sig_text: str | None = Field(default=None, max_length=500)
    evidence: Evidence = Field(min_length=1, max_length=50)


class ModelAllergy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    substance: str = Field(min_length=1, max_length=200)
    reaction: str | None = Field(default=None, max_length=200)
    evidence: Evidence = Field(min_length=1, max_length=50)


class ModelExtraction(BaseModel):
    """The only shape the model may answer with."""

    model_config = ConfigDict(extra="forbid")

    problems: list[ModelProblem] = Field(default_factory=list, max_length=200)
    medications: list[ModelMedication] = Field(default_factory=list, max_length=200)
    orders: list[ModelOrder] = Field(default_factory=list, max_length=200)
    allergies: list[ModelAllergy] = Field(default_factory=list, max_length=200)
    no_known_allergies: bool = False


SYSTEM_PROMPT = """You are a clinical documentation extraction agent.
Extract only facts explicitly stated in the encounter transcript. Do not infer diagnoses,
doses or orders that were not said. Every item must cite the indices of the utterances that
state it in "evidence". Medication "action" is start, continue, change, stop or reported
(patient-reported use). Only clinicians place orders. Put the spoken dose, route and
frequency for a medication or medication order verbatim in "sig_text".
Problem "status" is active, resolved, denied or historical.
Answer with a single JSON object that validates against this JSON Schema and nothing else:
"""

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _json_payload(text: str) -> str:
    stripped = _FENCE.sub("", text.strip())
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in model output")
    return stripped[start : end + 1]


@dataclass
class ModelRun:
    extraction: ClinicalExtraction | None
    findings: list[Finding] = field(default_factory=list)
    attempts: int = 0
    provider: str | None = None


class ExtractionAgent:
    def __init__(
        self,
        router: ModelRouter,
        guard: ContentGuard,
        formulary: Formulary,
        *,
        max_attempts: int = 3,
        max_tokens: int = 4096,
    ) -> None:
        self._router = router
        self._guard = guard
        self._formulary = formulary
        self._max_attempts = max_attempts
        self._max_tokens = max_tokens
        self._schema = json.dumps(ModelExtraction.model_json_schema(), separators=(",", ":"))

    @staticmethod
    def render_transcript(transcript: SegmentedTranscript) -> str:
        return "\n".join(f"[{u.index}] ({u.role.value}) {u.text}" for u in transcript.utterances)

    async def extract(self, transcript: SegmentedTranscript) -> ModelRun:
        messages: list[Message] = [
            Message(
                role=Role.SYSTEM, content=SYSTEM_PROMPT + self._schema, trust=TrustLevel.SYSTEM
            ),
            Message(
                role=Role.USER,
                content=self.render_transcript(transcript),
                trust=TrustLevel.EXTERNAL,
            ),
        ]
        run = ModelRun(extraction=None)
        for attempt in range(1, self._max_attempts + 1):
            run.attempts = attempt
            request = ModelRequest(
                messages=tuple(messages),
                max_tokens=self._max_tokens,
                temperature=0.0,
                classification=DataClassification.RESTRICTED,
                task="extract",
            )
            screened = self._guard.inspect_request(request)
            if screened.blocked or screened.request is None:
                run.findings.append(
                    Finding(
                        rule="prompt-injection-detected",
                        severity=Severity.WARNING,
                        message="transcript content was blocked by the content guard; model "
                        f"extraction skipped ({'; '.join(screened.reasons)})",
                    )
                )
                return run
            try:
                routed = await self._router.generate(screened.request)
            except ProviderUnavailableError as exc:
                run.findings.append(
                    Finding(
                        rule="model-unavailable",
                        severity=Severity.INFO,
                        message=f"no model provider could serve the request: {exc}",
                    )
                )
                return run
            run.provider = routed.response.provider
            output = self._guard.inspect_output(routed.response.text)
            if output.blocked or output.text is None:
                run.findings.append(
                    Finding(
                        rule="model-output-blocked",
                        severity=Severity.WARNING,
                        message="model output contained credential material and was discarded",
                    )
                )
                return run
            try:
                parsed = ModelExtraction.model_validate_json(_json_payload(output.text))
            except (ValueError, ValidationError) as exc:
                messages.append(Message(role=Role.ASSISTANT, content=output.text[:8000]))
                messages.append(
                    Message(
                        role=Role.USER,
                        content="Your previous answer did not validate against the schema: "
                        f"{_errors(exc)}. Answer again with only the corrected JSON object.",
                        trust=TrustLevel.SYSTEM,
                    )
                )
                continue
            run.extraction = self._to_domain(parsed, routed.response.model, attempt)
            return run
        run.findings.append(
            Finding(
                rule="model-output-invalid",
                severity=Severity.WARNING,
                message="model output failed schema validation after "
                f"{self._max_attempts} attempts",
            )
        )
        return run

    def _to_domain(self, parsed: ModelExtraction, model: str, attempts: int) -> ClinicalExtraction:
        def ev(values: list[int]) -> tuple[int, ...]:
            # Out-of-range citations are kept so grounding marks the item unsupported.
            return tuple(dict.fromkeys(values)) if values else ()

        problems = []
        for p in parsed.problems:
            concept = PROBLEM_LEXICON.by_name(p.name)
            problems.append(
                ProblemMention(
                    name=concept.display if concept else p.name,
                    coding=concept.codes if concept else (),
                    status=p.status,
                    evidence=ev(p.evidence),
                )
            )
        medications = []
        actions: dict[str, MedicationAction] = {}
        for m in parsed.medications:
            drug = self._formulary.resolve(m.name)
            sig = parse_sig(m.sig_text or m.name, self._formulary, drug=drug)
            name = drug.name if drug else m.name.strip().lower()
            actions[name] = m.action
            medications.append(
                MedicationMention(
                    name=name,
                    rxnorm=drug.rxnorm if drug else None,
                    sig=sig,
                    action=m.action,
                    evidence=ev(m.evidence),
                )
            )
        orders = []
        for o in parsed.orders:
            if o.kind is OrderKind.MEDICATION:
                drug = self._formulary.resolve(o.description) or (
                    self._formulary.resolve(o.sig_text) if o.sig_text else None
                )
                sig = parse_sig(o.sig_text or o.description, self._formulary, drug=drug)
                name = drug.name if drug else o.description
                orders.append(
                    OrderItem(
                        kind=o.kind,
                        description=o.sig_text or o.description,
                        medication=sig,
                        action=actions.get(name, MedicationAction.START),
                        evidence=ev(o.evidence),
                    )
                )
                continue
            lexicon = LAB_LEXICON if o.kind is OrderKind.LAB else IMAGING_LEXICON
            concept = (
                lexicon.by_name(o.description)
                if o.kind in {OrderKind.LAB, OrderKind.IMAGING}
                else None
            )
            orders.append(
                OrderItem(
                    kind=o.kind,
                    description=concept.display if concept else o.description,
                    coding=concept.codes if concept else (),
                    evidence=ev(o.evidence),
                )
            )
        allergies = tuple(
            AllergyMention(
                substance=normalize_substance(a.substance, self._formulary) or a.substance.lower(),
                reaction=a.reaction,
                evidence=ev(a.evidence),
            )
            for a in parsed.allergies
        )
        return ClinicalExtraction(
            problems=tuple(problems),
            medications=tuple(medications),
            orders=tuple(orders),
            allergies=allergies,
            no_known_allergies=parsed.no_known_allergies and not allergies,
            source="model",
            model=model,
            attempts=attempts,
        )


def _errors(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:10]
        )
    return str(exc)[:300]
