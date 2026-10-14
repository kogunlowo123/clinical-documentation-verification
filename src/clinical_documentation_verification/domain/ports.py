"""Ports the application layer depends on. Adapters live in infrastructure and integrations."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from clinical_documentation_verification.domain.models import (
    ChartSnapshot,
    FhirWriteResult,
    Verification,
    VerificationStatus,
)


class VerificationRepository(Protocol):
    async def add(self, verification: Verification) -> None: ...

    async def get(self, verification_id: str) -> Verification | None: ...

    async def find_by_idempotency_key(self, key: str) -> Verification | None: ...

    async def update(self, verification: Verification, *, expected_version: int) -> None: ...

    async def list(
        self,
        *,
        patient_id: str | None = None,
        status: VerificationStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Verification]: ...


class ChartSource(Protocol):
    async def chart(self, patient_id: str) -> ChartSnapshot: ...


class RecordWriter(Protocol):
    async def write(
        self, *, document_reference: dict[str, Any], provenance: dict[str, Any]
    ) -> FhirWriteResult: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
