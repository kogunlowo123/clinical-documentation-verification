"""Repository and full workflow against a real PostgreSQL (CI service container).

Runs when CDV_TEST_DATABASE_URL points at PostgreSQL, e.g.
postgresql+asyncpg://cdv:cdv@localhost:5432/cdv
"""

from __future__ import annotations

import os
import uuid

import pytest

from clinical_documentation_verification.application.service import VerificationService
from clinical_documentation_verification.clinical.formulary import Formulary
from clinical_documentation_verification.domain.errors import ConflictError
from clinical_documentation_verification.domain.models import VerificationStatus
from clinical_documentation_verification.infrastructure.database import (
    SqlVerificationRepository,
    SystemClock,
    create_schema,
    make_engine,
)
from tests.fakes import Upstreams
from tests.unit.test_service import ATTEST, build_verification, clinician, submit_cmd

URL = os.environ.get("CDV_TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL.startswith("postgresql"), reason="PostgreSQL not configured"),
]


async def test_repository_round_trip(formulary: Formulary) -> None:
    engine = make_engine(URL)
    await create_schema(engine)
    await create_schema(engine)  # idempotent
    repo = SqlVerificationRepository(engine)
    v = build_verification(formulary).model_copy(update={"idempotency_key": f"pg-{uuid.uuid4()}"})
    await repo.add(v)
    assert await repo.get(v.id) == v
    assert await repo.find_by_idempotency_key(v.idempotency_key or "") == v
    with pytest.raises(ConflictError):
        await repo.add(v.model_copy(update={"id": str(uuid.uuid4())}))
    approved = v.transition(VerificationStatus.APPROVED, at=SystemClock().now())
    await repo.update(approved, expected_version=v.version)
    with pytest.raises(ConflictError):
        await repo.update(approved, expected_version=v.version)
    assert any(
        x.id == v.id
        for x in await repo.list(patient_id="pat-1001", status=VerificationStatus.APPROVED)
    )
    await engine.dispose()


async def test_workflow_on_postgres(service: VerificationService, upstreams: Upstreams) -> None:
    v = await service.submit(submit_cmd(), clinician())
    written = await service.approve(v.id, ATTEST, clinician())
    assert written.status is VerificationStatus.WRITTEN
    assert upstreams.fhir.written("DocumentReference")
