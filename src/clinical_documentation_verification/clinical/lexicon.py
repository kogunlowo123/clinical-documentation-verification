"""Clinical concept lexicon: problems (SNOMED CT, ICD-10-CM), labs (LOINC), imaging, referrals.

Used by the deterministic extractor, by grounding checks on model output,
and to match transcript mentions against chart Conditions by code or name.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property

from clinical_documentation_verification.domain.models import Coding

SNOMED = "http://snomed.info/sct"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
LOINC = "http://loinc.org"


@dataclass(frozen=True)
class Concept:
    key: str
    display: str
    synonyms: tuple[str, ...]
    codes: tuple[Coding, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return (self.display.lower(), *self.synonyms)


def _problem(key: str, display: str, snomed: str, icd10: str, *synonyms: str) -> Concept:
    return Concept(
        key=key,
        display=display,
        synonyms=synonyms,
        codes=(
            Coding(system=SNOMED, code=snomed, display=display),
            Coding(system=ICD10, code=icd10, display=display),
        ),
    )


def _lab(key: str, display: str, loinc: str, *synonyms: str) -> Concept:
    return Concept(
        key=key,
        display=display,
        synonyms=synonyms,
        codes=(Coding(system=LOINC, code=loinc, display=display),),
    )


PROBLEMS: tuple[Concept, ...] = (
    _problem(
        "hypertension",
        "Hypertension",
        "38341003",
        "I10",
        "high blood pressure",
        "htn",
        "essential hypertension",
    ),
    _problem(
        "type-2-diabetes",
        "Type 2 diabetes mellitus",
        "44054006",
        "E11.9",
        "type 2 diabetes",
        "type ii diabetes",
        "diabetes type 2",
        "t2dm",
        "diabetes",
        "diabetes mellitus",
        "dm2",
    ),
    _problem(
        "type-1-diabetes",
        "Type 1 diabetes mellitus",
        "46635009",
        "E10.9",
        "type 1 diabetes",
        "type i diabetes",
        "t1dm",
    ),
    _problem(
        "hyperlipidemia",
        "Hyperlipidemia",
        "55822004",
        "E78.5",
        "high cholesterol",
        "dyslipidemia",
        "hypercholesterolemia",
    ),
    _problem("asthma", "Asthma", "195967001", "J45.909"),
    _problem(
        "copd",
        "Chronic obstructive pulmonary disease",
        "13645005",
        "J44.9",
        "copd",
        "emphysema",
        "chronic bronchitis",
    ),
    _problem(
        "heart-failure",
        "Heart failure",
        "84114007",
        "I50.9",
        "congestive heart failure",
        "chf",
        "hfref",
        "hfpef",
    ),
    _problem(
        "atrial-fibrillation", "Atrial fibrillation", "49436004", "I48.91", "afib", "a-fib", "a fib"
    ),
    _problem(
        "ckd",
        "Chronic kidney disease",
        "709044004",
        "N18.9",
        "ckd",
        "chronic renal disease",
        "kidney disease",
    ),
    _problem(
        "hypothyroidism",
        "Hypothyroidism",
        "40930008",
        "E03.9",
        "underactive thyroid",
        "low thyroid",
    ),
    _problem(
        "depression",
        "Depressive disorder",
        "35489007",
        "F32.A",
        "depression",
        "major depression",
        "mdd",
    ),
    _problem(
        "anxiety", "Anxiety disorder", "197480006", "F41.9", "anxiety", "generalized anxiety", "gad"
    ),
    _problem(
        "gerd",
        "Gastroesophageal reflux disease",
        "235595009",
        "K21.9",
        "gerd",
        "acid reflux",
        "reflux",
        "heartburn",
    ),
    _problem(
        "osteoarthritis", "Osteoarthritis", "396275006", "M19.90", "degenerative joint disease"
    ),
    _problem("pneumonia", "Pneumonia", "233604007", "J18.9", "community acquired pneumonia"),
    _problem("uti", "Urinary tract infection", "68566005", "N39.0", "uti", "bladder infection"),
    _problem("migraine", "Migraine", "37796009", "G43.909", "migraines", "migraine headache"),
    _problem("obesity", "Obesity", "414916001", "E66.9", "obese"),
    _problem(
        "cad",
        "Coronary artery disease",
        "53741008",
        "I25.10",
        "cad",
        "coronary disease",
        "ischemic heart disease",
    ),
    _problem(
        "dvt",
        "Deep venous thrombosis",
        "128053003",
        "I82.409",
        "dvt",
        "deep vein thrombosis",
        "blood clot in the leg",
    ),
    _problem(
        "low-back-pain",
        "Low back pain",
        "279039007",
        "M54.50",
        "back pain",
        "lower back pain",
        "lumbago",
    ),
    _problem("chest-pain", "Chest pain", "29857009", "R07.9"),
    _problem("nausea", "Nausea", "422587007", "R11.0", "nauseous", "nauseated"),
)

LABS: tuple[Concept, ...] = (
    _lab(
        "hba1c", "Hemoglobin A1c", "4548-4", "a1c", "hba1c", "hemoglobin a1c", "glycated hemoglobin"
    ),
    _lab("cbc", "Complete blood count", "58410-2", "cbc", "blood count", "complete blood count"),
    _lab("bmp", "Basic metabolic panel", "51990-0", "bmp", "basic metabolic panel", "chem 7"),
    _lab("cmp", "Comprehensive metabolic panel", "24323-8", "cmp", "comprehensive metabolic panel"),
    _lab(
        "lipid-panel",
        "Lipid panel",
        "24331-1",
        "lipid panel",
        "lipids",
        "cholesterol panel",
        "fasting lipids",
    ),
    _lab(
        "tsh", "Thyroid stimulating hormone", "3016-3", "tsh", "thyroid function", "thyroid levels"
    ),
    _lab("urinalysis", "Urinalysis", "24356-8", "urinalysis", "ua", "urine test"),
    _lab("inr", "INR", "6301-6", "inr", "pt/inr", "protime"),
    _lab("bnp", "B-type natriuretic peptide", "30934-4", "bnp", "natriuretic peptide"),
    _lab("creatinine", "Creatinine", "2160-0", "creatinine", "kidney function"),
    _lab("potassium", "Potassium", "2823-3", "potassium level"),
)

IMAGING: tuple[Concept, ...] = (
    Concept("chest-xray", "Chest X-ray", ("chest x-ray", "chest xray", "cxr", "chest film")),
    Concept("ct", "CT scan", ("ct scan", "cat scan", "ct of the", "ct chest", "ct head")),
    Concept("mri", "MRI", ("mri",)),
    Concept("ultrasound", "Ultrasound", ("ultrasound", "sonogram", "doppler")),
    Concept("echocardiogram", "Echocardiogram", ("echocardiogram", "echo", "tte")),
    Concept(
        "ecg",
        "Electrocardiogram",
        ("ecg", "ekg", "electrocardiogram"),
        (Coding(system=LOINC, code="11524-6", display="EKG study"),),
    ),
    Concept("mammogram", "Mammogram", ("mammogram", "mammography")),
    Concept("dexa", "Bone density scan", ("dexa", "bone density")),
)

SPECIALTIES: tuple[str, ...] = (
    "cardiology",
    "endocrinology",
    "nephrology",
    "pulmonology",
    "gastroenterology",
    "neurology",
    "orthopedics",
    "dermatology",
    "psychiatry",
    "physical therapy",
    "ophthalmology",
    "urology",
    "rheumatology",
    "oncology",
    "podiatry",
    "nutrition",
)


@dataclass(frozen=True)
class Match:
    concept: Concept
    start: int
    end: int


@dataclass(frozen=True)
class Lexicon:
    concepts: tuple[Concept, ...]

    @cached_property
    def _by_name(self) -> dict[str, Concept]:
        return {name: c for c in self.concepts for name in c.names}

    @cached_property
    def _by_code(self) -> dict[tuple[str, str], Concept]:
        return {(code.system, code.code): c for c in self.concepts for code in c.codes}

    @cached_property
    def _pattern(self) -> re.Pattern[str]:
        names = sorted(self._by_name, key=len, reverse=True)
        return re.compile(
            r"(?<![\w-])(?:" + "|".join(re.escape(n) for n in names) + r")(?![\w-])", re.I
        )

    def find(self, text: str) -> list[Match]:
        return [
            Match(self._by_name[m.group(0).lower()], m.start(), m.end())
            for m in self._pattern.finditer(text)
        ]

    def by_name(self, name: str) -> Concept | None:
        key = name.strip().lower()
        direct = self._by_name.get(key)
        if direct is not None:
            return direct
        found = self.find(key)
        return found[0].concept if found else None

    def by_codes(self, codings: Iterable[Coding]) -> Concept | None:
        for coding in codings:
            concept = self._by_code.get((coding.system, coding.code))
            if concept is not None:
                return concept
        return None


PROBLEM_LEXICON = Lexicon(PROBLEMS)
LAB_LEXICON = Lexicon(LABS)
IMAGING_LEXICON = Lexicon(IMAGING)
_SPECIALTY = re.compile(
    r"\b(?:refer(?:ral|ring)?|send (?:you|her|him) to|see)\b[^.?!]{0,40}?\b("
    + "|".join(re.escape(s) for s in SPECIALTIES)
    + r")\b",
    re.I,
)


def find_referrals(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(1).lower(), m.start(1), m.end(1)) for m in _SPECIALTY.finditer(text)]
