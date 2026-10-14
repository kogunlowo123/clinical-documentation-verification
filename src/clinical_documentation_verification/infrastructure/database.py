"""SQLAlchemy Core persistence. PostgreSQL in production; any async dialect works."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from clinical_documentation_verification.domain.errors import ConflictError
from clinical_documentation_verification.domain.models import Verification, VerificationStatus

metadata = MetaData()

verifications = Table(
    "verifications",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("patient_id", String(64), nullable=False),
    Column("encounter_id", String(64), nullable=False),
    Column("status", String(24), nullable=False),
    Column("band", String(16), nullable=False),
    Column("score", Float, nullable=False),
    Column("idempotency_key", String(128), nullable=True, unique=True),
    Column("version", Integer, nullable=False),
    Column("document", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Index("ix_verifications_patient", "patient_id", "created_at"),
    Index("ix_verifications_status", "status"),
)

schema_version = Table(
    "schema_version",
    metadata,
    Column("version", Integer, primary_key=True),
)

SCHEMA_VERSION = 1


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        current = (await conn.execute(select(schema_version.c.version))).scalar()
        if current is None:
            await conn.execute(insert(schema_version).values(version=SCHEMA_VERSION))


class SqlVerificationRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @staticmethod
    def _row(v: Verification) -> dict[str, object]:
        return {
            "status": v.status.value,
            "band": v.consistency.band.value,
            "score": v.consistency.score,
            "version": v.version,
            "document": v.model_dump_json(),
            "updated_at": v.updated_at,
        }

    async def add(self, verification: Verification) -> None:
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    insert(verifications).values(
                        id=verification.id,
                        patient_id=verification.patient_id,
                        encounter_id=verification.encounter_id,
                        idempotency_key=verification.idempotency_key,
                        created_at=verification.created_at,
                        **self._row(verification),
                    )
                )
        except IntegrityError as exc:
            raise ConflictError("a verification with this id or idempotency key exists") from exc

    async def get(self, verification_id: str) -> Verification | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(verifications.c.document).where(verifications.c.id == verification_id)
                )
            ).first()
        return Verification.model_validate_json(row[0]) if row else None

    async def find_by_idempotency_key(self, key: str) -> Verification | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(verifications.c.document).where(verifications.c.idempotency_key == key)
                )
            ).first()
        return Verification.model_validate_json(row[0]) if row else None

    async def update(self, verification: Verification, *, expected_version: int) -> None:
        """Optimistic concurrency: the write succeeds only against the version that was read."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(verifications)
                .where(verifications.c.id == verification.id)
                .where(verifications.c.version == expected_version)
                .values(**self._row(verification))
            )
            if result.rowcount == 0:
                raise ConflictError(f"verification {verification.id} was modified concurrently")

    async def list(
        self,
        *,
        patient_id: str | None = None,
        status: VerificationStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Verification]:
        query = (
            select(verifications.c.document)
            .order_by(verifications.c.created_at.desc(), verifications.c.id)
            .limit(limit)
            .offset(offset)
        )
        if patient_id is not None:
            query = query.where(verifications.c.patient_id == patient_id)
        if status is not None:
            query = query.where(verifications.c.status == status.value)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [Verification.model_validate_json(r[0]) for r in rows]


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
