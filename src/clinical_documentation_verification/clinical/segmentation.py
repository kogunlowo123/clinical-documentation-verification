"""Transcript segmentation by speaker and encounter section.

Input is either a labelled plain-text transcript (``Dr. Lee: ...``,
``[00:01:12] Patient: ...``) or pre-diarized utterances from an ASR system.
Output is a sequence of utterances with a resolved speaker role and the
encounter section each belongs to.

Speaker roles are resolved in order: explicit mapping supplied by the
caller, the label vocabulary (Doctor, NP, Patient, Mother, Interpreter...),
then, for anonymous diarization labels such as ``Speaker 1``, a cue-phrase
score that identifies the clinician (the speaker who examines, orders and
prescribes) and the patient.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter

from clinical_documentation_verification.domain.models import (
    RawUtterance,
    Section,
    SegmentedTranscript,
    SpeakerRole,
    TranscriptInput,
    Utterance,
)

_LINE = re.compile(
    r"^\s*(?:\[(?P<ts>\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)\]\s*)?"
    r"(?P<label>[A-Za-z][A-Za-z0-9 .'\-]{0,40}?)\s*(?:\([^)]{1,40}\))?\s*:\s*(?P<text>.*)$"
)

_ROLE_WORDS: tuple[tuple[SpeakerRole, re.Pattern[str]], ...] = (
    (
        SpeakerRole.CLINICIAN,
        re.compile(
            r"^(?:dr\.?|doctor|physician|clinician|provider|attending|resident|np|pa|pa-c|"
            r"nurse practitioner|physician assistant|nurse|rn|md|do)(?:\s|$)",
            re.I,
        ),
    ),
    (SpeakerRole.PATIENT, re.compile(r"^(?:patient|pt)(?:\s|$)", re.I)),
    (
        SpeakerRole.CAREGIVER,
        re.compile(
            r"^(?:caregiver|mother|mom|father|dad|parent|daughter|son|wife|husband|spouse|"
            r"partner|guardian|family member|sister|brother)(?:\s|$)",
            re.I,
        ),
    ),
    (SpeakerRole.INTERPRETER, re.compile(r"^(?:interpreter|translator)(?:\s|$)", re.I)),
)
_ANONYMOUS = re.compile(r"^(?:speaker|spk|voice|participant)\s*[-_]?\s*\w{1,3}$", re.I)
_TITLED = re.compile(r"^(?:mr|mrs|ms|miss|mx)\.?\s+[A-Z][a-z'\-]+$")

_CLINICIAN_CUES = re.compile(
    r"\b(?:i(?:'m| am) going to (?:start|order|prescribe|increase|decrease|stop|refer)|"
    r"let's (?:start|order|get|check|increase|decrease|stop|continue)|"
    r"i(?:'d| would) like (?:you|to)|i'll (?:order|prescribe|send|refer)|prescrib\w*|"
    r"exam(?:ine)?|listen to your|your blood pressure is|on exam|follow up|"
    r"what brings you|any allergies|are you taking|how have you been)\b",
    re.I,
)
_PATIENT_CUES = re.compile(
    r"\b(?:i(?:'ve| have) been|i feel|i felt|it hurts|my (?:\w+ )?(?:pain|chest|back|head|"
    r"stomach|knee)|i take|i'm taking|i stopped|i ran out)\b",
    re.I,
)

_SECTION_CUES: tuple[tuple[Section, re.Pattern[str]], ...] = (
    (
        Section.ASSESSMENT_PLAN,
        re.compile(
            r"\b(?:assessment|plan|diagnos\w*|i(?:'m| am) going to|let's (?:start|order|get|check|"
            r"increase|decrease|stop)|i'll (?:order|prescribe|send|refer)|we(?:'ll| will) "
            r"(?:start|order|check|get))\b",
            re.I,
        ),
    ),
    (
        Section.OBJECTIVE,
        re.compile(
            r"\b(?:on exam|examine|exam|listen to your|vitals|blood pressure is|heart rate is|"
            r"lungs (?:sound|are)|temperature is|let me (?:look|check|feel))\b",
            re.I,
        ),
    ),
    (
        Section.MEDICATIONS,
        re.compile(
            r"\b(?:medications?|meds|are you (?:still )?taking|refills?|pharmacy|prescriptions?)\b",
            re.I,
        ),
    ),
    (
        Section.SUBJECTIVE,
        re.compile(
            r"\b(?:what brings you|how have you been|tell me about|any (?:other )?symptoms)\b", re.I
        ),
    ),
)


def _role_for_label(label: str) -> SpeakerRole | None:
    for role, pattern in _ROLE_WORDS:
        if pattern.match(label.strip()):
            return role
    return None


def _is_speaker_label(label: str, declared: dict[str, SpeakerRole]) -> bool:
    label = label.strip()
    return (
        label in declared
        or _role_for_label(label) is not None
        or bool(_ANONYMOUS.match(label))
        or bool(_TITLED.match(label))
    )


def _timestamp(value: str | None) -> float | None:
    if not value:
        return None
    parts = [float(p) for p in value.split(":")]
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def parse_text(text: str, declared: dict[str, SpeakerRole]) -> list[RawUtterance]:
    """Split a labelled transcript into turns; unlabelled lines continue the current turn."""
    turns: list[RawUtterance] = []
    speaker: str | None = None
    start: float | None = None
    buffer: list[str] = []

    def flush() -> None:
        body = " ".join(part for part in buffer if part).strip()
        if speaker is not None and body:
            turns.append(RawUtterance(speaker=speaker, text=body[:10_000], start_s=start))

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _LINE.match(line)
        if m and _is_speaker_label(m.group("label"), declared):
            flush()
            speaker = m.group("label").strip()
            start = _timestamp(m.group("ts"))
            buffer = [m.group("text").strip()]
        elif speaker is None:
            speaker, start, buffer = "Unattributed", None, [line]
        else:
            buffer.append(line)
    flush()
    return turns


def resolve_roles(
    turns: list[RawUtterance], declared: dict[str, SpeakerRole]
) -> dict[str, SpeakerRole]:
    roles: dict[str, SpeakerRole] = {}
    unresolved: list[str] = []
    for label in dict.fromkeys(t.speaker for t in turns):
        role = declared.get(label) or _role_for_label(label)
        if role is None and label.lower() == "unattributed":
            role = SpeakerRole.OTHER
        if role is None:
            unresolved.append(label)
        else:
            roles[label] = role

    if unresolved:
        roles.update(_infer_roles(turns, unresolved, known=roles))
    return roles


def _infer_roles(
    turns: list[RawUtterance], unresolved: list[str], *, known: dict[str, SpeakerRole]
) -> dict[str, SpeakerRole]:
    """Assign anonymous diarization labels by clinician and patient cue-phrase scores."""
    clinician_score: Counter[str] = Counter()
    patient_score: Counter[str] = Counter()
    for turn in turns:
        if turn.speaker in unresolved:
            clinician_score[turn.speaker] += len(_CLINICIAN_CUES.findall(turn.text))
            patient_score[turn.speaker] += len(_PATIENT_CUES.findall(turn.text))
    inferred: dict[str, SpeakerRole] = {}
    if SpeakerRole.CLINICIAN not in known.values():
        ranked = sorted(unresolved, key=lambda s: (-clinician_score[s], unresolved.index(s)))
        if clinician_score[ranked[0]] > 0:
            inferred[ranked[0]] = SpeakerRole.CLINICIAN
    has_patient = SpeakerRole.PATIENT in known.values()
    for label in unresolved:
        if label in inferred:
            continue
        if not has_patient and patient_score[label] >= clinician_score[label]:
            inferred[label] = SpeakerRole.PATIENT
            has_patient = True
        else:
            inferred[label] = SpeakerRole.OTHER
    return inferred


def _section_for(text: str, role: SpeakerRole, current: Section) -> Section:
    if role is not SpeakerRole.CLINICIAN:
        return current
    for section, pattern in _SECTION_CUES:
        if pattern.search(text):
            return section
    return current


def segment(transcript: TranscriptInput) -> SegmentedTranscript:
    declared = dict(transcript.speaker_roles)
    if transcript.text is not None:
        raw = parse_text(transcript.text, declared)
    else:
        raw = list(transcript.utterances or ())
    turns = raw
    roles = resolve_roles(turns, declared)

    section = Section.SUBJECTIVE
    utterances: list[Utterance] = []
    for index, turn in enumerate(turns):
        role = roles[turn.speaker]
        section = _section_for(turn.text, role, section)
        utterances.append(
            Utterance(
                index=index,
                speaker=turn.speaker,
                role=role,
                section=section,
                text=turn.text,
                start_s=turn.start_s,
            )
        )
    canonical = "\n".join(f"{u.speaker}: {u.text}" for u in utterances)
    return SegmentedTranscript(
        utterances=tuple(utterances),
        speakers=roles,
        digest="sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
    )
