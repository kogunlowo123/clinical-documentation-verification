"""Deterministic clinical extraction from a segmented transcript.

Lexicon- and rule-based: drug mentions from the formulary, problem mentions
from the clinical lexicon with NegEx assertion status, sig parsing for
dose/route/frequency, action cues (start/stop/change/continue), speaker-role
rules (only clinicians order), lab/imaging/referral orders, allergies and
"no known drug allergies" statements.

It runs on every verification. When a foundation model is configured its
output is cross-checked against this extraction; when none is configured
(or the model output fails validation) this extraction is used directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from clinical_documentation_verification.clinical.formulary import Drug, Formulary
from clinical_documentation_verification.clinical.lexicon import (
    IMAGING_LEXICON,
    LAB_LEXICON,
    PROBLEM_LEXICON,
    Concept,
    find_referrals,
)
from clinical_documentation_verification.clinical.negation import assertion_status
from clinical_documentation_verification.clinical.sig import parse_sig
from clinical_documentation_verification.domain.models import (
    AllergyMention,
    ClinicalExtraction,
    MedicationAction,
    MedicationMention,
    MedicationSig,
    MentionStatus,
    OrderItem,
    OrderKind,
    ProblemMention,
    SegmentedTranscript,
    SpeakerRole,
    Utterance,
)

_SENTENCE = re.compile(r"[^.;!?]+[.;!?]?")
_ACTION_CUES: tuple[tuple[MedicationAction, re.Pattern[str]], ...] = (
    (
        MedicationAction.STOP,
        re.compile(
            r"\b(?:stop(?:ped|ping)?|discontinu\w*|d/c|hold(?:ing)?|come off|quit|"
            r"no longer tak\w*|not taking|don't take|ran out of)\b",
            re.I,
        ),
    ),
    (
        MedicationAction.CHANGE,
        re.compile(
            r"\b(?:increas\w*|decreas\w*|reduc\w*|bump(?:ing)? (?:it |that )?up|go up|"
            r"titrat\w*|chang\w* (?:the|your) dose|switch\w*|lower(?:ing)? (?:the|your) dose|"
            r"doubl\w*)\b",
            re.I,
        ),
    ),
    (
        MedicationAction.START,
        re.compile(
            r"\b(?:start\w*|begin|prescrib\w*|add(?:ing)?|put you on|i'll send|new prescription|"
            r"trial of|give you|order(?:ing)?)\b",
            re.I,
        ),
    ),
    (
        MedicationAction.CONTINUE,
        re.compile(r"\b(?:continu\w*|keep (?:taking|on)|stay on|refill\w*|same dose)\b", re.I),
    ),
)
_PRECEDENCE = {
    MedicationAction.REPORTED: 0,
    MedicationAction.CONTINUE: 1,
    MedicationAction.START: 2,
    MedicationAction.CHANGE: 3,
    MedicationAction.STOP: 4,
}
_ORDER_CUE = re.compile(
    r"\b(?:order\w*|check\w*|get(?:ting)?|draw\w*|repeat\w*|send\w*|obtain\w*|run|schedul\w*|"
    r"need(?:s)?|do an?|recheck\w*)\b",
    re.I,
)
_NKDA = re.compile(
    r"\b(?:no known (?:drug |medication )?allergies|nkda|no (?:drug |medication )?allergies|"
    r"not allergic to anything|no allergies to medications?)\b",
    re.I,
)
_ALLERGY_TO = re.compile(
    r"\ballerg(?:ic|y|ies)\s+to\s+(?P<s>[a-z][a-z0-9\- ]{1,40}?)"
    r"(?=$|[.,;!?]|\s+(?:and|but|it|which|that|causes?|caused|gave|gives|makes|made|"
    r"since|when|because)\b)",
    re.I,
)
_ALLERGY_BEFORE = re.compile(r"\b(?P<s>[a-z][a-z\-]{2,30})\s+allerg(?:y|ies)\b", re.I)
_REACTION = re.compile(
    r"\b(?:gives? me|gave me|causes?|caused|broke out in|got|get|had)\s+(?:a |an |bad )?"
    r"(?P<r>rash|hives|anaphylaxis|swelling|itching|nausea|vomiting|shortness of breath|"
    r"throat swelling|angioedema)\b",
    re.I,
)
_SUBSTANCE_NOISE = re.compile(
    r"\b(?:drugs?|medications?|meds|antibiotics?|the|a|an|of|class|some|certain)\b", re.I
)
_AVOID = re.compile(r"\b(?:avoid\w*|stay away from|steer clear of)\b", re.I)
_NOT_SUBSTANCES = frozenset({"no", "any", "anything", "nothing", "known", "drug", "food"})


def _sentences(text: str) -> list[tuple[str, int]]:
    return [(m.group(0), m.start()) for m in _SENTENCE.finditer(text) if m.group(0).strip()]


def _action(clause: str, role: SpeakerRole) -> MedicationAction:
    for action, pattern in _ACTION_CUES:
        if pattern.search(clause):
            if action in {MedicationAction.START, MedicationAction.CHANGE} and (
                role is not SpeakerRole.CLINICIAN
            ):
                return MedicationAction.REPORTED
            return action
    return MedicationAction.REPORTED


@dataclass
class _MedAccumulator:
    drug: Drug
    action: MedicationAction
    sig: MedicationSig | None
    evidence: list[int] = field(default_factory=list)
    clinician_action: bool = False


@dataclass
class _ProblemAccumulator:
    concept: Concept
    status: MentionStatus
    evidence: list[int] = field(default_factory=list)
    by_clinician: bool = False


def _supersedes(acc: _MedAccumulator, action: MedicationAction, is_clinician: bool) -> bool:
    """Whether a new statement about a medication replaces what was said before.

    Clinician decisions outrank patient reports; among statements of equal
    authority the stronger action wins, and a clinician may restart a
    medication that was stopped earlier in the encounter.
    """
    clinician_decision = is_clinician and action is not MedicationAction.REPORTED
    if clinician_decision and not acc.clinician_action:
        return True
    if acc.clinician_action and not clinician_decision:
        return False
    if acc.action is MedicationAction.STOP and action is MedicationAction.START:
        return True
    return _PRECEDENCE[action] >= _PRECEDENCE[acc.action]


def normalize_substance(raw: str, formulary: Formulary) -> str:
    cleaned = _SUBSTANCE_NOISE.sub(" ", raw.lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return ""
    if cleaned in formulary.allergy_classes:
        return cleaned
    drug = formulary.resolve(cleaned)
    return drug.name if drug else cleaned


class DeterministicExtractor:
    def __init__(self, formulary: Formulary) -> None:
        self._formulary = formulary

    def extract(self, transcript: SegmentedTranscript) -> ClinicalExtraction:
        meds: dict[str, _MedAccumulator] = {}
        problems: dict[str, _ProblemAccumulator] = {}
        orders: list[OrderItem] = []
        allergies: dict[str, AllergyMention] = {}
        nkda = False
        for utterance in transcript.utterances:
            for sentence, _ in _sentences(utterance.text):
                self._medications(utterance, sentence, meds)
                self._problems(utterance, sentence, problems)
                if utterance.role is SpeakerRole.CLINICIAN:
                    orders.extend(self._orders(utterance, sentence))
                found, said_nkda = self._allergies(utterance, sentence)
                nkda = nkda or said_nkda
                for allergy in found:
                    allergies.setdefault(allergy.substance, allergy)

        medications = tuple(
            MedicationMention(
                name=acc.drug.name,
                rxnorm=acc.drug.rxnorm,
                sig=acc.sig,
                action=acc.action,
                evidence=tuple(dict.fromkeys(acc.evidence)),
            )
            for acc in meds.values()
        )
        med_orders = tuple(
            OrderItem(
                kind=OrderKind.MEDICATION,
                description=m.sig.text if m.sig else m.name,
                medication=m.sig,
                action=m.action,
                evidence=m.evidence,
            )
            for m in medications
            if m.action in {MedicationAction.START, MedicationAction.CHANGE}
        )
        return ClinicalExtraction(
            problems=tuple(
                ProblemMention(
                    name=acc.concept.display,
                    coding=acc.concept.codes,
                    status=acc.status,
                    evidence=tuple(dict.fromkeys(acc.evidence)),
                )
                for acc in problems.values()
            ),
            medications=medications,
            orders=(*med_orders, *_dedupe_orders(orders)),
            allergies=tuple(allergies.values()),
            no_known_allergies=nkda and not allergies,
            source="deterministic",
            attempts=1,
        )

    # ------------------------------------------------------------------ medications

    def _medications(
        self, utterance: Utterance, sentence: str, meds: dict[str, _MedAccumulator]
    ) -> None:
        mentions = self._formulary.find(sentence)
        for position, (drug, start, end) in enumerate(mentions):
            clause_start = mentions[position - 1][2] if position else 0
            clause_end = (
                mentions[position + 1][1] if position + 1 < len(mentions) else len(sentence)
            )
            clause = sentence[clause_start:clause_end]
            window = sentence[start:clause_end]
            status = assertion_status(sentence, start, end)
            action = _action(clause, utterance.role)
            avoided = bool(_AVOID.search(clause))
            if (status is MentionStatus.DENIED or avoided) and action is not MedicationAction.STOP:
                continue  # "avoid ibuprofen", "no aspirin": not a medication the patient is on
            sig = parse_sig(window, self._formulary, drug=drug)
            if sig.dose is None and sig.frequency is None and sig.route is None:
                sig = parse_sig(clause, self._formulary, drug=drug)
            has_detail = sig.dose is not None or sig.frequency is not None
            acc = meds.get(drug.name)
            is_clinician = utterance.role is SpeakerRole.CLINICIAN
            if acc is None:
                meds[drug.name] = _MedAccumulator(
                    drug=drug,
                    action=action,
                    sig=sig if has_detail else None,
                    evidence=[utterance.index],
                    clinician_action=is_clinician and action is not MedicationAction.REPORTED,
                )
                continue
            acc.evidence.append(utterance.index)
            if _supersedes(acc, action, is_clinician):
                acc.action = action
                acc.clinician_action = acc.clinician_action or (
                    is_clinician and action is not MedicationAction.REPORTED
                )
                if has_detail:
                    acc.sig = sig
            elif has_detail and acc.sig is None:
                acc.sig = sig

    # --------------------------------------------------------------------- problems

    def _problems(
        self, utterance: Utterance, sentence: str, problems: dict[str, _ProblemAccumulator]
    ) -> None:
        for match in PROBLEM_LEXICON.find(sentence):
            status = assertion_status(sentence, match.start, match.end)
            key = match.concept.key
            is_clinician = utterance.role is SpeakerRole.CLINICIAN
            acc = problems.get(key)
            if acc is None:
                problems[key] = _ProblemAccumulator(
                    match.concept, status, [utterance.index], is_clinician
                )
                continue
            acc.evidence.append(utterance.index)
            if is_clinician or not acc.by_clinician:
                acc.status = status
                acc.by_clinician = acc.by_clinician or is_clinician

    # ----------------------------------------------------------------------- orders

    def _orders(self, utterance: Utterance, sentence: str) -> list[OrderItem]:
        if not _ORDER_CUE.search(sentence):
            found: list[OrderItem] = []
        else:
            found = [
                OrderItem(
                    kind=OrderKind.LAB,
                    description=m.concept.display,
                    coding=m.concept.codes,
                    evidence=(utterance.index,),
                )
                for m in LAB_LEXICON.find(sentence)
                if assertion_status(sentence, m.start, m.end) is not MentionStatus.DENIED
            ]
            found.extend(
                OrderItem(
                    kind=OrderKind.IMAGING,
                    description=m.concept.display,
                    coding=m.concept.codes,
                    evidence=(utterance.index,),
                )
                for m in IMAGING_LEXICON.find(sentence)
                if assertion_status(sentence, m.start, m.end) is not MentionStatus.DENIED
            )
        found.extend(
            OrderItem(
                kind=OrderKind.REFERRAL,
                description=f"Referral to {specialty}",
                evidence=(utterance.index,),
            )
            for specialty, _, _ in find_referrals(sentence)
        )
        return found

    # -------------------------------------------------------------------- allergies

    def _allergies(self, utterance: Utterance, sentence: str) -> tuple[list[AllergyMention], bool]:
        if _NKDA.search(sentence):
            return [], True
        reaction_match = _REACTION.search(sentence)
        reaction = reaction_match.group("r").lower() if reaction_match else None
        raw = [m.group("s") for m in _ALLERGY_TO.finditer(sentence)]
        raw.extend(m.group("s") for m in _ALLERGY_BEFORE.finditer(sentence))
        found: list[AllergyMention] = []
        for candidate in raw:
            substance = normalize_substance(candidate, self._formulary)
            if len(substance) < 3 or substance in _NOT_SUBSTANCES:
                continue
            found.append(
                AllergyMention(substance=substance, reaction=reaction, evidence=(utterance.index,))
            )
        return found, False


def _dedupe_orders(orders: list[OrderItem]) -> list[OrderItem]:
    seen: dict[tuple[OrderKind, str], OrderItem] = {}
    for order in orders:
        key = (order.kind, order.description.lower())
        if key in seen:
            previous = seen[key]
            seen[key] = previous.model_copy(
                update={"evidence": tuple(dict.fromkeys((*previous.evidence, *order.evidence)))}
            )
        else:
            seen[key] = order
    return list(seen.values())
