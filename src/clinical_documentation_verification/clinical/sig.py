"""Medication instruction (sig) parsing.

Turns free text such as ``"ibuprofen 2 tablets of 200 mg by mouth every 6-8 hours as
needed for pain"`` into structured dose, unit, route, form, frequency and PRN
fields that order-safety rules can check. Handles spoken forms ("twice a
day", "at bedtime", "two puffs"), clinical abbreviations (BID, q6h, PO, SC),
ranges (upper bound kept for safety checks), tablet-count times strength,
and flags abbreviations on The Joint Commission "Do Not Use" list.
"""

from __future__ import annotations

import re
from fractions import Fraction

from clinical_documentation_verification.clinical.formulary import Drug, Formulary
from clinical_documentation_verification.domain.models import Dose, Frequency, MedicationSig, Route

_WORDS = {
    "a half": 0.5,
    "one half": 0.5,
    "one-half": 0.5,
    "half": 0.5,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fifteen": 15,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
}
_NUM = (
    r"(?:\d+(?:,\d{3})*(?:\.\d+)?|\.\d+|\d+/\d+|"
    + "|".join(re.escape(w) for w in sorted(_WORDS, key=len, reverse=True))
    + r")"
)

_UNITS: dict[str, str] = {
    "mg": "mg",
    "milligram": "mg",
    "milligrams": "mg",
    "mcg": "mcg",
    "µg": "mcg",
    "ug": "mcg",
    "microgram": "mcg",
    "micrograms": "mcg",
    "g": "g",
    "gm": "g",
    "gram": "g",
    "grams": "g",
    "ml": "mL",
    "milliliter": "mL",
    "milliliters": "mL",
    "cc": "mL",
    "unit": "unit",
    "units": "unit",
    "u": "unit",
    "iu": "unit",
    "international units": "unit",
    "meq": "mEq",
    "puff": "puff",
    "puffs": "puff",
    "tablet": "tablet",
    "tablets": "tablet",
    "tab": "tablet",
    "tabs": "tablet",
    "capsule": "capsule",
    "capsules": "capsule",
    "cap": "capsule",
    "caps": "capsule",
    "drop": "drop",
    "drops": "drop",
    "gtt": "drop",
    "gtts": "drop",
    "spray": "spray",
    "sprays": "spray",
    "patch": "patch",
    "patches": "patch",
}
_COUNT_UNITS = frozenset({"tablet", "capsule", "puff", "drop", "spray", "patch"})
MASS_IN_MG = {"mg": 1.0, "g": 1000.0, "mcg": 0.001}

_UNIT_ALT = "|".join(re.escape(u) for u in sorted(_UNITS, key=len, reverse=True))
_AMOUNT = re.compile(
    rf"(?<![\w.])(?P<lo>{_NUM})(?:\s*(?:-|to|or)\s*(?P<hi>{_NUM}))?\s*(?P<unit>{_UNIT_ALT})(?![\w])",
    re.IGNORECASE,
)

_ROUTES: tuple[tuple[Route, re.Pattern[str]], ...] = tuple(
    (route, re.compile(pattern, re.IGNORECASE))
    for route, pattern in (
        (Route.NG, r"\b(?:via\s+)?(?:ng|nasogastric|g-?tube|peg)(?:\s+tube)?\b"),
        (Route.SL, r"\b(?:sl|sublingual(?:ly)?|under the tongue)\b"),
        (Route.PO, r"\b(?:po|p\.o\.|by mouth|orally|oral|per os|swallow)(?![\w])"),
        (Route.IV, r"\b(?:iv|i\.v\.|intravenous(?:ly)?|iv push|ivpb|infusion)\b"),
        (Route.IM, r"\b(?:im|i\.m\.|intramuscular(?:ly)?)\b"),
        (Route.SC, r"\b(?:sc|sq|subq|sub-q|subcut|subcutaneous(?:ly)?)\b"),
        (Route.PR, r"\b(?:pr|per rectum|rectal(?:ly)?)\b"),
        (Route.INH, r"\b(?:inh|inhaled|inhalation|inhaler|nebuli[sz]ed|nebuli[sz]er|neb|mdi)\b"),
        (Route.TD, r"\b(?:td|transdermal(?:ly)?)\b"),
        (Route.TOP, r"\b(?:topical(?:ly)?|apply to (?:the )?skin)\b"),
        (Route.OPH, r"\b(?:ophthalmic|in (?:the|each|both) eyes?)\b"),
        (Route.OTIC, r"\b(?:otic|in (?:the|each|both) ears?)\b"),
        (Route.NASAL, r"\b(?:intranasal(?:ly)?|nasal(?:ly)?|in (?:each|both) nostrils?)\b"),
    )
)

_FORMS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (form, re.compile(pattern, re.IGNORECASE))
    for form, pattern in (
        ("tablet", r"\b(?:tablets?|tabs?)\b"),
        ("capsule", r"\b(?:capsules?|caps?)\b"),
        ("oral-liquid", r"\b(?:solution|syrup|suspension|elixir|liquid)\b"),
        ("injection", r"\b(?:injection|injectable|vial|pen|syringe)\b"),
        ("suppository", r"\bsuppositor(?:y|ies)\b"),
        ("patch", r"\bpatch(?:es)?\b"),
        ("inhaler", r"\b(?:inhaler|mdi|nebuli[sz]er solution)\b"),
        ("topical", r"\b(?:cream|ointment|gel|lotion)\b"),
        ("ophthalmic", r"\beye drops?\b"),
        ("otic", r"\bear drops?\b"),
        ("nasal", r"\bnasal spray\b"),
    )
)

FORM_ROUTES: dict[str, frozenset[Route]] = {
    "tablet": frozenset({Route.PO, Route.SL, Route.NG}),
    "capsule": frozenset({Route.PO}),
    "oral-liquid": frozenset({Route.PO, Route.NG}),
    "injection": frozenset({Route.IV, Route.IM, Route.SC}),
    "suppository": frozenset({Route.PR}),
    "patch": frozenset({Route.TD}),
    "inhaler": frozenset({Route.INH}),
    "topical": frozenset({Route.TOP}),
    "ophthalmic": frozenset({Route.OPH}),
    "otic": frozenset({Route.OTIC}),
    "nasal": frozenset({Route.NASAL}),
}

_N = _NUM
_FREQUENCIES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), code)
    for pattern, code in (
        (
            rf"\b(?:q\.?\s?|every\s+)(?P<a>{_N})\s*(?:-|to)\s*(?P<b>{_N})\s*(?:h|hr|hrs|hours?)\b",
            "QRANGE",
        ),
        (rf"\b(?:q\.?\s?(?P<n>\d+)\s*(?:h|hr|hrs|hours?)|every\s+(?P<w>{_N})\s+hours?)\b", "QNH"),
        (r"\b(?:q\.?\s?h|every hour|hourly)\b", "Q1H"),
        (r"\b(?:q\.?\s?i\.?\s?d\.?|four times (?:a|per|each) day|4 times (?:a|per) day)\b", "QID"),
        (r"\b(?:t\.?\s?i\.?\s?d\.?|three times (?:a|per|each) day|3 times (?:a|per) day)\b", "TID"),
        (
            r"\b(?:b\.?\s?i\.?\s?d\.?|twice (?:a |per |each )?day|twice daily"
            r"|two times (?:a|per) day|2 times (?:a|per) day|morning and (?:evening|night))\b",
            "BID",
        ),
        (rf"\b(?P<t>{_N})\s+times\s+(?:a|per|each)\s+day\b", "NXD"),
        (r"\b(?:q\.?\s?h\.?\s?s\.?|at bedtime|nightly|every night|at night|before bed)\b", "QHS"),
        (r"\b(?:q\.?\s?am|every morning|in the morning|each morning)\b", "QAM"),
        (r"\b(?:every other day|q\.?\s?o\.?\s?d\.?|alternate days)\b", "QOD"),
        (r"\b(?:weekly|once (?:a |per )?week|every week|q\.?\s?wk|q\.?\s?week)\b", "WEEKLY"),
        (
            r"\b(?:once daily|once (?:a |per )?day|daily|every day|each day|q\.?\s?d\.?|qday"
            r"|a day)\b",
            "DAILY",
        ),
        (r"\b(?:once|x\s?1|one time|single dose|stat)\b", "ONCE"),
    )
)

_FIXED: dict[str, tuple[float, float | None]] = {
    "Q1H": (24.0, 1.0),
    "QID": (4.0, 6.0),
    "TID": (3.0, 8.0),
    "BID": (2.0, 12.0),
    "QHS": (1.0, 24.0),
    "QAM": (1.0, 24.0),
    "QOD": (0.5, 48.0),
    "WEEKLY": (1.0 / 7.0, 168.0),
    "DAILY": (1.0, 24.0),
}

_PRN = re.compile(r"\b(?:p\.?r\.?n\.?|as needed|as-needed|when needed|if needed)\b", re.I)
_INDICATION = re.compile(
    r"\b(?:p\.?r\.?n\.?|as needed|when needed|if needed)\s+(?:for\s+)?"
    r"(?P<what>[a-z][a-z \-]{2,40}?)"
    r"(?=$|[.,;]|\s+(?:and|then|for \d))",
    re.I,
)

# The Joint Commission "Do Not Use" list (NPSG / IM.02.02.01).
_DO_NOT_USE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("U for unit", re.compile(r"(?<=\d)\s?U\b|\b\d+\s+U\b")),
    ("IU for international unit", re.compile(r"\bI\.?U\.?\b")),
    ("Q.D. for daily", re.compile(r"\bq\.?d\.?(?![a-z])", re.I)),
    ("Q.O.D. for every other day", re.compile(r"\bq\.?o\.?d\.?(?![a-z])", re.I)),
    ("trailing zero", re.compile(r"(?<![\d.])\d+\.0(?!\d)\s*(?:mg|mcg|g|ml|units?)\b", re.I)),
    ("missing leading zero", re.compile(r"(?<![\d])\.\d+\s*(?:mg|mcg|g|ml|units?)\b", re.I)),
    ("MS/MSO4/MgSO4", re.compile(r"\b(?:MS|MSO4|MgSO4)\b")),
)


def parse_number(token: str) -> float:
    cleaned = token.strip().lower().replace(",", "")
    if cleaned in _WORDS:
        return float(_WORDS[cleaned])
    if "/" in cleaned:
        return float(Fraction(cleaned))
    return float(cleaned)


def to_unit(value: float, unit: str, target: str) -> float | None:
    """Convert between mass units; identical units pass through; otherwise None."""
    if unit == target:
        return value
    if unit in MASS_IN_MG and target in MASS_IN_MG:
        return value * MASS_IN_MG[unit] / MASS_IN_MG[target]
    return None


def _amounts(text: str) -> list[tuple[float, float | None, str, int]]:
    found = []
    for m in _AMOUNT.finditer(text):
        unit_key = m.group("unit").lower()
        unit = _UNITS.get(unit_key, _UNITS.get(m.group("unit")))
        if unit is None:  # pragma: no cover - alternation is built from _UNITS
            continue
        try:
            lo = parse_number(m.group("lo"))
            hi = parse_number(m.group("hi")) if m.group("hi") else None
        except (ValueError, ZeroDivisionError):
            continue
        if lo <= 0 or (hi is not None and hi <= 0):
            continue
        found.append((lo, hi, unit, m.start()))
    return found


def parse_dose(text: str, *, preferred_unit: str | None = None) -> Dose | None:
    """Dose administered per occasion; count units times strength when both appear.

    ``preferred_unit`` is the unit the drug's limits are kept in; when it is a
    count unit (puffs, tablets of a fixed combination) the count is the dose.
    """
    amounts = _amounts(text)
    if not amounts:
        return None
    counts = [a for a in amounts if a[2] in _COUNT_UNITS]
    measured = [a for a in amounts if a[2] not in _COUNT_UNITS]
    preferred = [a for a in amounts if a[2] == preferred_unit]
    if preferred_unit in _COUNT_UNITS and preferred:
        lo, hi, unit, _ = preferred[0]
        return Dose(value=hi or lo, unit=unit, min_value=lo if hi else None)
    if counts and measured:
        count_lo, count_hi, count_unit, _ = counts[0]
        strength, _, unit, _ = measured[0]
        if count_unit in {"tablet", "capsule"}:
            upper = (count_hi or count_lo) * strength
            lower = count_lo * strength if count_hi else None
            return Dose(value=upper, unit=unit, min_value=lower)
    lo, hi, unit, _ = measured[0] if measured else counts[0]
    return Dose(value=hi or lo, unit=unit, min_value=lo if hi else None)


def parse_route(text: str) -> Route | None:
    best: tuple[int, Route] | None = None
    for route, pattern in _ROUTES:
        m = pattern.search(text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), route)
    return best[1] if best else None


def parse_form(text: str) -> str | None:
    for form, pattern in _FORMS:
        if pattern.search(text):
            return form
    return None


def parse_frequency(text: str) -> Frequency | None:
    for pattern, code in _FREQUENCIES:
        m = pattern.search(text)
        if m is None:
            continue
        if code == "QRANGE":
            a, b = parse_number(m.group("a")), parse_number(m.group("b"))
            shortest = min(a, b)
            if shortest <= 0:
                continue
            return Frequency(
                code=f"Q{_fmt(min(a, b))}-{_fmt(max(a, b))}H",
                doses_per_day=24.0 / shortest,
                interval_hours=shortest,
            )
        if code == "QNH":
            hours = parse_number(m.group("n") or m.group("w"))
            if hours <= 0:
                continue
            return Frequency(
                code=f"Q{_fmt(hours)}H", doses_per_day=24.0 / hours, interval_hours=hours
            )
        if code == "NXD":
            times = parse_number(m.group("t"))
            if times <= 0:
                continue
            return Frequency(
                code=f"{_fmt(times)}XD", doses_per_day=times, interval_hours=24 / times
            )
        if code == "ONCE":
            return Frequency(code="ONCE", doses_per_day=1.0, one_time=True)
        per_day, interval = _FIXED[code]
        return Frequency(code=code, doses_per_day=per_day, interval_hours=interval)
    return None


def _fmt(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:g}"


def quality_flags(text: str) -> tuple[str, ...]:
    return tuple(name for name, pattern in _DO_NOT_USE if pattern.search(text))


def parse_sig(text: str, formulary: Formulary, *, drug: Drug | None = None) -> MedicationSig:
    """Parse a medication instruction. ``drug`` pins the ingredient when already known."""
    resolved = drug or formulary.resolve(text)
    prn = bool(_PRN.search(text))
    indication = None
    if prn:
        m = _INDICATION.search(text)
        indication = m.group("what").strip().lower() if m else None
    return MedicationSig(
        text=text.strip(),
        drug=resolved.name if resolved else None,
        rxnorm=resolved.rxnorm if resolved else None,
        dose=parse_dose(text, preferred_unit=resolved.unit if resolved else None),
        route=parse_route(text),
        form=parse_form(text),
        frequency=parse_frequency(text),
        prn=prn,
        indication=indication,
        quality_flags=quality_flags(text),
    )


def dose_in(sig: MedicationSig, unit: str) -> float | None:
    """Per-administration dose expressed in ``unit``, or None when not comparable."""
    if sig.dose is None:
        return None
    return to_unit(sig.dose.value, sig.dose.unit, unit)


def daily_amount(sig: MedicationSig, unit: str) -> float | None:
    """Maximum amount per 24 h implied by the sig, in ``unit``."""
    single = dose_in(sig, unit)
    if single is None or sig.frequency is None or sig.frequency.doses_per_day is None:
        return None
    per_day = 1.0 if sig.frequency.one_time else sig.frequency.doses_per_day
    return single * per_day if per_day >= 1 else single
