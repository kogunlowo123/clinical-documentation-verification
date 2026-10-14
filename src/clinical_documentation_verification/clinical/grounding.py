"""Grounding: every extracted item must be supported by the utterances it cites.

A model that invents a medication or problem not present in the transcript
produces an item whose cited utterances do not mention it. Such items are
kept (the clinician should see what the model claimed) but marked
``grounded=False``, lower the consistency score and raise a finding.
"""

from __future__ import annotations

import re

from clinical_documentation_verification.clinical.formulary import Formulary
from clinical_documentation_verification.clinical.lexicon import (
    IMAGING_LEXICON,
    LAB_LEXICON,
    PROBLEM_LEXICON,
    Lexicon,
)
from clinical_documentation_verification.domain.models import (
    ClinicalExtraction,
    OrderKind,
    SegmentedTranscript,
)

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset({"of", "the", "a", "an", "to", "and", "for", "with", "referral", "type"})


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _mentions(text: str, names: tuple[str, ...]) -> bool:
    lowered = text.lower()
    for name in names:
        if re.search(rf"(?<![\w-]){re.escape(name.lower())}(?![\w-])", lowered):
            return True
    return False


def _valid(evidence: tuple[int, ...], size: int) -> bool:
    return bool(evidence) and all(0 <= i < size for i in evidence)


class Grounder:
    def __init__(self, formulary: Formulary) -> None:
        self._formulary = formulary

    def _drug_names(self, name: str) -> tuple[str, ...]:
        drug = self._formulary.resolve(name)
        return drug.names if drug else (name,)

    @staticmethod
    def _concept_names(lexicon: Lexicon, name: str) -> tuple[str, ...]:
        concept = lexicon.by_name(name)
        return concept.names if concept else (name,)

    def _supported(
        self, evidence: tuple[int, ...], names: tuple[str, ...], t: SegmentedTranscript
    ) -> bool:
        if not _valid(evidence, len(t.utterances)):
            return False
        cited = t.text_of(evidence)
        if _mentions(cited, names):
            return True
        # Multi-word descriptions: every content word of some name must appear in the cited text.
        said = _words(cited)
        return any(
            bool(content) and content <= said
            for content in (_words(name) - _STOPWORDS for name in names)
        )

    def ground(
        self, extraction: ClinicalExtraction, transcript: SegmentedTranscript
    ) -> ClinicalExtraction:
        problems = tuple(
            p.model_copy(
                update={
                    "grounded": self._supported(
                        p.evidence, self._concept_names(PROBLEM_LEXICON, p.name), transcript
                    )
                }
            )
            for p in extraction.problems
        )
        medications = tuple(
            m.model_copy(
                update={
                    "grounded": self._supported(m.evidence, self._drug_names(m.name), transcript)
                }
            )
            for m in extraction.medications
        )
        orders = []
        for order in extraction.orders:
            if order.source == "proposed":
                orders.append(order)
                continue
            if order.kind is OrderKind.MEDICATION:
                drug = order.medication.drug if order.medication and order.medication.drug else None
                names = self._drug_names(drug or order.description)
            elif order.kind is OrderKind.LAB:
                names = self._concept_names(LAB_LEXICON, order.description)
            elif order.kind is OrderKind.IMAGING:
                names = self._concept_names(IMAGING_LEXICON, order.description)
            else:
                names = (order.description.lower().removeprefix("referral to ").strip(),)
            orders.append(
                order.model_copy(
                    update={"grounded": self._supported(order.evidence, names, transcript)}
                )
            )
        allergies = tuple(
            a.model_copy(
                update={
                    "grounded": self._supported(
                        a.evidence, (*self._drug_names(a.substance), a.substance), transcript
                    )
                }
            )
            for a in extraction.allergies
        )
        return extraction.model_copy(
            update={
                "problems": problems,
                "medications": medications,
                "orders": tuple(orders),
                "allergies": allergies,
            }
        )
