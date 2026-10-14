"""Assertion status of a clinical mention: NegEx-style negation, history and resolution cues.

Follows the NegEx approach (Chapman et al., 2001): trigger phrases before or
after a concept negate it when no scope terminator ("but", "however",
sentence punctuation) intervenes and the trigger is within a short token
window. Historical and resolved cues are handled the same way.
"""

from __future__ import annotations

import re

from clinical_documentation_verification.domain.models import MentionStatus

_PRE_NEGATION = (
    "no history of",
    "no evidence of",
    "negative for",
    "never been diagnosed with",
    "never had",
    "does not have",
    "doesn't have",
    "do not have",
    "don't have",
    "haven't had",
    "has not had",
    "have not had",
    "absence of",
    "free of",
    "rules out",
    "ruled out",
    "denies",
    "denied",
    "denying",
    "without",
    "not",
    "no",
)
_POST_NEGATION = ("was ruled out", "is ruled out", "ruled out", "unlikely", "was negative")
_RESOLVED_PRE = ("no longer has", "no longer have", "resolved", "recovered from", "got over")
_RESOLVED_POST = ("has resolved", "resolved", "went away", "is gone", "is cured", "in remission")
_HISTORY_PRE = (
    "past medical history of",
    "history of",
    "hx of",
    "h/o",
    "previously had",
    "used to have",
    "had a",
    "in the past",
)
_TERMINATORS = frozenset(
    {"but", "however", "although", "though", "except", "aside", "apart", "which", "whereas"}
)
_PRE_WINDOW = 6
_POST_WINDOW = 4
_TOKEN = re.compile(r"[a-z0-9'/\-]+|[.;:!?]", re.I)


def _tokens(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(0).lower(), m.start(), m.end()) for m in _TOKEN.finditer(text)]


def _scope(tokens: list[tuple[str, int, int]], *, before: bool, limit: int) -> list[str]:
    window: list[str] = []
    sequence = reversed(tokens) if before else iter(tokens)
    for token, _, _ in sequence:
        if token in _TERMINATORS or token in {".", ";", "!", "?"}:
            break
        window.append(token)
        if len(window) >= limit:
            break
    return list(reversed(window)) if before else window


def _contains(window: list[str], phrases: tuple[str, ...]) -> bool:
    joined = f" {' '.join(window)} "
    return any(f" {phrase} " in joined for phrase in phrases)


def assertion_status(text: str, start: int, end: int) -> MentionStatus:
    """Status of the concept spanning ``text[start:end]``."""
    tokens = _tokens(text)
    before = [t for t in tokens if t[2] <= start]
    after = [t for t in tokens if t[1] >= end]
    pre = _scope(before, before=True, limit=_PRE_WINDOW)
    post = _scope(after, before=False, limit=_POST_WINDOW)
    if _contains(pre, _RESOLVED_PRE) or _contains(post, _RESOLVED_POST):
        return MentionStatus.RESOLVED
    if _contains(pre, _PRE_NEGATION) or _contains(post, _POST_NEGATION):
        return MentionStatus.DENIED
    if _contains(pre, _HISTORY_PRE):
        return MentionStatus.HISTORICAL
    return MentionStatus.ACTIVE
