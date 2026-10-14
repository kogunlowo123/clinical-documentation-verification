"""Medication sig parsing and the medication reference table."""

from __future__ import annotations

from pathlib import Path

import pytest

from clinical_documentation_verification.clinical.formulary import Formulary
from clinical_documentation_verification.clinical.sig import (
    daily_amount,
    dose_in,
    parse_dose,
    parse_form,
    parse_frequency,
    parse_number,
    parse_route,
    parse_sig,
    quality_flags,
    to_unit,
)
from clinical_documentation_verification.domain.models import MedicationSig, Route


@pytest.mark.parametrize(
    ("text", "drug", "dose", "unit", "route", "freq", "prn", "per_day_mg"),
    [
        ("metformin 500 mg PO BID", "metformin", 500, "mg", Route.PO, "BID", False, 1000),
        (
            "Lisinopril 10mg by mouth once daily",
            "lisinopril",
            10,
            "mg",
            Route.PO,
            "DAILY",
            False,
            10,
        ),
        (
            "acetaminophen 650 mg every 6 hours as needed for pain",
            "acetaminophen",
            650,
            "mg",
            None,
            "Q6H",
            True,
            2600,
        ),
        ("ceftriaxone 1 g IV q24h", "ceftriaxone", 1, "g", Route.IV, "Q24H", False, 1000),
        (
            "take two tablets of ibuprofen 200 mg three times a day",
            "ibuprofen",
            400,
            "mg",
            None,
            "TID",
            False,
            1200,
        ),
        (
            "metformin 1,000 mg twice a day with meals",
            "metformin",
            1000,
            "mg",
            None,
            "BID",
            False,
            2000,
        ),
        ("ondansetron 4 mg IV q8h prn nausea", "ondansetron", 4, "mg", Route.IV, "Q8H", True, 12),
        (
            "levothyroxine 88 mcg PO every morning",
            "levothyroxine",
            88,
            "mcg",
            Route.PO,
            "QAM",
            False,
            0.088,
        ),
        ("amoxicillin 875 mg PO q12h", "amoxicillin", 875, "mg", Route.PO, "Q12H", False, 1750),
        (
            "tramadol 50 to 100 mg PO every 4-6 hours prn",
            "tramadol",
            100,
            "mg",
            Route.PO,
            "Q4-6H",
            True,
            600,
        ),
        ("azithromycin 1 gram PO once", "azithromycin", 1, "g", Route.PO, "ONCE", False, 1000),
        ("aspirin 81 mg daily", "aspirin", 81, "mg", None, "DAILY", False, 81),
        ("Tylenol half gram four times a day", "acetaminophen", 0.5, "g", None, "QID", False, 2000),
        ("naproxen 500 mg 2 times per day", "naproxen", 500, "mg", None, "BID", False, 1000),
        ("gabapentin 300 mg three times per day", "gabapentin", 300, "mg", None, "TID", False, 900),
        (
            "hydromorphone 0.5 mg IV every hour as needed",
            "hydromorphone",
            0.5,
            "mg",
            Route.IV,
            "Q1H",
            True,
            12,
        ),
        ("vitamin d 1000 units every other day", None, 1000, "unit", None, "QOD", False, None),
        ("methotrexate 15 mg weekly", None, 15, "mg", None, "WEEKLY", False, 15),
        ("sertraline 50 mg 1 times a day", "sertraline", 50, "mg", None, "1XD", False, 50),
        ("prednisone 40 mg every eight hours", "prednisone", 40, "mg", None, "Q8H", False, 120),
    ],
)
def test_parse_sig(
    formulary: Formulary,
    *,
    text: str,
    drug: str | None,
    dose: float,
    unit: str,
    route: Route | None,
    freq: str,
    prn: bool,
    per_day_mg: float | None,
) -> None:
    sig = parse_sig(text, formulary)
    assert sig.drug == drug
    assert sig.dose is not None
    assert sig.dose.value == pytest.approx(dose)
    assert sig.dose.unit == unit
    assert sig.route is route
    assert sig.frequency is not None
    assert sig.frequency.code == freq
    assert sig.prn is prn
    if per_day_mg is not None:
        assert daily_amount(sig, "mg") == pytest.approx(per_day_mg)


def test_count_units_preferred_for_count_based_drugs(formulary: Formulary) -> None:
    puffs = parse_sig("albuterol 90 mcg 2 puffs inhaled q4h prn wheeze", formulary)
    assert puffs.dose is not None
    assert (puffs.dose.value, puffs.dose.unit) == (2, "puff")
    assert puffs.route is Route.INH
    assert puffs.indication == "wheeze"
    tabs = parse_sig("Bactrim DS 1 tablet PO BID", formulary)
    assert tabs.drug == "sulfamethoxazole-trimethoprim"
    assert tabs.dose is not None
    assert tabs.dose.unit == "tablet"
    ranged = parse_sig("ibuprofen 1-2 tablets of 200 mg q6h", formulary)
    assert ranged.dose is not None
    assert (ranged.dose.value, ranged.dose.min_value) == (400, 200)


@pytest.mark.parametrize(
    ("text", "flags"),
    [
        ("Lantus 10 U qd", {"U for unit", "Q.D. for daily"}),
        ("morphine .5 mg IV q2h", {"missing leading zero"}),
        ("warfarin 5.0 mg daily", {"trailing zero"}),
        ("give MSO4 2 mg IV", {"MS/MSO4/MgSO4"}),
        ("vitamin D 1000 IU daily", {"IU for international unit"}),
        ("furosemide 20 mg qod", {"Q.O.D. for every other day"}),
        ("lisinopril 10 mg PO daily", set()),
    ],
)
def test_do_not_use_abbreviations(text: str, flags: set[str]) -> None:
    assert set(quality_flags(text)) == flags


@pytest.mark.parametrize(
    ("text", "route"),
    [
        ("under the tongue", Route.SL),
        ("via NG tube", Route.NG),
        ("given IM", Route.IM),
        ("subcutaneously", Route.SC),
        ("per rectum", Route.PR),
        ("transdermal patch", Route.TD),
        ("apply topically", Route.TOP),
        ("one drop in each eye", Route.OPH),
        ("two drops in the ear", Route.OTIC),
        ("one spray in each nostril", Route.NASAL),
        ("nebulized", Route.INH),
        ("no route here", None),
    ],
)
def test_routes(text: str, route: Route | None) -> None:
    assert parse_route(text) is route


@pytest.mark.parametrize(
    ("text", "form"),
    [
        ("2 tabs", "tablet"),
        ("capsule", "capsule"),
        ("oral suspension", "oral-liquid"),
        ("prefilled syringe", "injection"),
        ("rectal suppository", "suppository"),
        ("nicotine patch", "patch"),
        ("rescue inhaler", "inhaler"),
        ("hydrocortisone cream", "topical"),
        ("eye drops", "ophthalmic"),
        ("ear drops", "otic"),
        ("nasal spray", "nasal"),
        ("nothing", None),
    ],
)
def test_forms(text: str, form: str | None) -> None:
    assert parse_form(text) == form


def test_numbers_units_and_edge_cases(formulary: Formulary) -> None:
    assert parse_number("1/2") == 0.5
    assert parse_number("twelve") == 12
    assert parse_number("1,500") == 1500
    assert to_unit(1, "g", "mg") == 1000
    assert to_unit(5, "mL", "mg") is None
    assert to_unit(3, "unit", "unit") == 3
    assert parse_dose("no numbers") is None
    assert parse_dose("0 mg") is None
    assert parse_dose("1/0 mg") is None
    assert parse_frequency("q0h") is None
    assert parse_frequency("every 0 to 4 hours") is None
    assert parse_frequency("0 times per day") is None
    assert parse_frequency("whenever") is None
    bare = MedicationSig(text="x")
    assert dose_in(bare, "mg") is None
    assert daily_amount(bare, "mg") is None
    weekly = parse_sig("methotrexate 15 mg weekly", formulary)
    assert daily_amount(weekly, "mg") == 15
    assert parse_sig("ibuprofen 400 mg", formulary).complete is False
    assert parse_sig("ibuprofen 400 mg PO q6h", formulary).complete is True


class TestFormulary:
    def test_bundled_table(self, formulary: Formulary) -> None:
        assert formulary.version
        assert formulary.get("29046") is not None
        assert formulary.get("Zestril") == formulary.get("lisinopril")
        assert formulary.get("unknownium") is None
        assert formulary.resolve("start insulin glargine 10 units") == formulary.get(
            "insulin glargine"
        )
        assert formulary.resolve("nothing relevant") is None
        names = [d.name for d, _, _ in formulary.find("lantus and metformin and aspirin")]
        assert names == ["insulin glargine", "metformin", "aspirin"]

    def test_route_limits(self, formulary: Formulary) -> None:
        metoprolol = formulary.get("metoprolol")
        assert metoprolol is not None
        assert metoprolol.limits(Route.IV).max_single_dose == 5
        assert metoprolol.limits(Route.PO).max_single_dose == 400
        assert metoprolol.limits(None).max_daily_dose == 450

    def test_allergy_rules(self, formulary: Formulary) -> None:
        assert "penicillin" in formulary.allergy_rule("Penicillin").contraindicated
        assert "cephalosporin" in formulary.allergy_rule("penicillin").cross_reactive
        assert formulary.allergy_rule("amoxicillin").contraindicated == frozenset({"penicillin"})
        assert formulary.allergy_rule("lisinopril").contraindicated == frozenset()
        assert formulary.allergy_rule("latex").contraindicated == frozenset()

    def test_duplicate_names_rejected(self) -> None:
        text = """
version: "1"
drugs:
  - {name: a, rxnorm: "1", synonyms: [b], unit: mg, routes: [PO]}
  - {name: b, rxnorm: "2", unit: mg, routes: [PO]}
"""
        with pytest.raises(ValueError, match="listed twice"):
            Formulary.from_yaml(text)

    def test_from_file(self, tmp_path: Path) -> None:
        path = tmp_path / "f.yaml"
        path.write_text(
            'version: "x"\ndrugs:\n'
            '  - {name: zz, rxnorm: "9", unit: mg, routes: [PO], max_single_dose: 5}\n',
            encoding="utf-8",
        )
        drug = Formulary.from_file(path).get("zz")
        assert drug is not None
        assert drug.max_single_dose == 5
