"""Medication reference table: names, RxNorm codes, dose ceilings, routes and classes."""

from __future__ import annotations

import re
from functools import cached_property
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from clinical_documentation_verification.domain.models import Route, Severity


class DoseLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_single_dose: float | None = Field(default=None, gt=0)
    max_daily_dose: float | None = Field(default=None, gt=0)


class Drug(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    rxnorm: str = Field(pattern=r"^\d{1,10}$")
    synonyms: tuple[str, ...] = ()
    classes: frozenset[str] = frozenset()
    schedule: Literal["CII", "CIII", "CIV", "CV"] | None = None
    unit: Literal["mg", "mcg", "unit", "puff", "tablet"]
    max_single_dose: float | None = Field(default=None, gt=0)
    max_daily_dose: float | None = Field(default=None, gt=0)
    routes: frozenset[Route]
    route_limits: dict[Route, DoseLimits] = Field(default_factory=dict)

    def limits(self, route: Route | None) -> DoseLimits:
        if route is not None and route in self.route_limits:
            return self.route_limits[route]
        return DoseLimits(max_single_dose=self.max_single_dose, max_daily_dose=self.max_daily_dose)

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.synonyms)


class DuplicationGroup(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    classes: frozenset[str]
    severity: Severity


class Interaction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    classes: tuple[str, str]
    severity: Severity
    message: str


class AllergyClass(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contraindicated: frozenset[str] = frozenset()
    cross_reactive: frozenset[str] = frozenset()


class Formulary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    drugs: tuple[Drug, ...] = Field(min_length=1)
    duplication_groups: dict[str, DuplicationGroup] = Field(default_factory=dict)
    interactions: tuple[Interaction, ...] = ()
    allergy_classes: dict[str, AllergyClass] = Field(default_factory=dict)

    @field_validator("drugs")
    @classmethod
    def _unique_names(cls, drugs: tuple[Drug, ...]) -> tuple[Drug, ...]:
        seen: set[str] = set()
        for drug in drugs:
            for name in drug.names:
                key = name.lower()
                if key in seen:
                    raise ValueError(f"drug name {name!r} is listed twice")
                seen.add(key)
        return drugs

    @classmethod
    def from_yaml(cls, text: str) -> Formulary:
        return cls.model_validate(yaml.safe_load(text))

    @classmethod
    def from_file(cls, path: Path) -> Formulary:
        return cls.from_yaml(path.read_text(encoding="utf-8"))

    @classmethod
    def bundled(cls) -> Formulary:
        text = (
            resources.files("clinical_documentation_verification.clinical")
            .joinpath("data/formulary.yaml")
            .read_text(encoding="utf-8")
        )
        return cls.from_yaml(text)

    @cached_property
    def _by_name(self) -> dict[str, Drug]:
        return {name.lower(): drug for drug in self.drugs for name in drug.names}

    @cached_property
    def _by_rxnorm(self) -> dict[str, Drug]:
        return {drug.rxnorm: drug for drug in self.drugs}

    @cached_property
    def _pattern(self) -> re.Pattern[str]:
        # Longest names first so "insulin glargine" wins over "glargine".
        names = sorted(self._by_name, key=len, reverse=True)
        alternation = "|".join(re.escape(n) for n in names)
        return re.compile(rf"(?<![\w-])(?:{alternation})(?![\w-])", re.IGNORECASE)

    def get(self, name_or_code: str) -> Drug | None:
        key = name_or_code.strip().lower()
        return self._by_name.get(key) or self._by_rxnorm.get(key)

    def find(self, text: str) -> list[tuple[Drug, int, int]]:
        """Every drug mention in ``text`` as (drug, start, end), in order of appearance."""
        return [
            (self._by_name[m.group(0).lower()], m.start(), m.end())
            for m in self._pattern.finditer(text)
        ]

    def resolve(self, text: str) -> Drug | None:
        """Exact name/code match first, otherwise the first drug mentioned in the text."""
        exact = self.get(text)
        if exact is not None:
            return exact
        found = self.find(text)
        return found[0][0] if found else None

    def allergy_rule(self, substance: str) -> AllergyClass:
        """Classes contraindicated or cross-reactive for a documented allergy."""
        key = substance.strip().lower()
        if key in self.allergy_classes:
            return self.allergy_classes[key]
        drug = self.resolve(key)
        if drug is not None:
            # A named-drug allergy contraindicates that drug's own classes only when the
            # class is an allergy class (e.g. penicillin), otherwise just the ingredient.
            classes = frozenset(c for c in drug.classes if c in _ALLERGY_RELEVANT_CLASSES)
            return AllergyClass(contraindicated=classes, cross_reactive=frozenset())
        return AllergyClass()


_ALLERGY_RELEVANT_CLASSES = frozenset(
    {"penicillin", "cephalosporin", "sulfonamide-antibiotic", "fluoroquinolone", "macrolide"}
)
