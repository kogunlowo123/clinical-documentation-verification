"""Latency budgets for hot paths. Loose enough for shared CI runners."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from clinical_documentation_verification.clinical.consistency import analyse
from clinical_documentation_verification.clinical.extraction import DeterministicExtractor
from clinical_documentation_verification.clinical.formulary import Formulary
from clinical_documentation_verification.clinical.orders import OrderValidator
from clinical_documentation_verification.clinical.segmentation import segment
from clinical_documentation_verification.clinical.sig import parse_sig
from clinical_documentation_verification.domain.models import OrderItem, OrderKind, TranscriptInput
from tests.conftest import CLEAN_ENCOUNTER, ENCOUNTER, clinician_headers, submission
from tests.fakes import FakeIdp

pytestmark = pytest.mark.performance


def long_transcript(repeats: int) -> str:
    return "".join(ENCOUNTER.replace("[", "\n[") for _ in range(repeats))


def test_sig_parsing_budget(formulary: Formulary) -> None:
    sigs = [
        "metformin 500 mg PO BID",
        "acetaminophen 650 mg q6h prn pain",
        "insulin glargine 20 units SC qhs",
    ]
    started = time.perf_counter()
    for _ in range(300):
        for text in sigs:
            parse_sig(text, formulary)
    per_parse_ms = (time.perf_counter() - started) * 1000 / 900
    assert per_parse_ms < 2


def test_rule_pipeline_budget_for_a_long_encounter(formulary: Formulary) -> None:
    """A 30-minute visit (~150 utterances) is segmented, extracted, checked and scored quickly."""
    text = long_transcript(15)
    started = time.perf_counter()
    transcript = segment(TranscriptInput(text=text))
    extraction = DeterministicExtractor(formulary).extract(transcript)
    findings = OrderValidator(formulary).validate(extraction.orders)
    analyse(extraction, reference=None, diff=(), findings=findings, formulary=formulary)
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert len(transcript.utterances) >= 100
    assert elapsed_ms < 1500


def test_order_validation_budget(formulary: Formulary) -> None:
    orders = [
        OrderItem(kind=OrderKind.MEDICATION, description=t, medication=parse_sig(t, formulary))
        for t in ["lisinopril 10 mg PO daily", "apixaban 5 mg PO BID", "ibuprofen 400 mg PO q6h"]
        * 10
    ]
    started = time.perf_counter()
    for _ in range(20):
        OrderValidator(formulary).validate(orders)
    assert (time.perf_counter() - started) * 1000 / 20 < 50


def test_submission_round_trip_budget(client: TestClient, clinician_idp: FakeIdp) -> None:
    headers = clinician_headers(clinician_idp)
    client.post("/v1/verifications", json=submission(CLEAN_ENCOUNTER), headers=headers)  # warm-up
    samples = []
    for _ in range(10):
        started = time.perf_counter()
        response = client.post(
            "/v1/verifications", json=submission(CLEAN_ENCOUNTER), headers=headers
        )
        samples.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 201
    samples.sort()
    assert samples[int(len(samples) * 0.9) - 1] < 750  # p90 in-process, SQLite, fake FHIR
