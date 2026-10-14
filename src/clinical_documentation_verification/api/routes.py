"""HTTP routes.

Every route except discovery and operations requires a bearer token: an
enterprise OIDC token for clinicians or an identity-fabric credential for
agents. Authorization is decided per action by the application service.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from clinical_documentation_verification.api import a2a
from clinical_documentation_verification.api.schemas import (
    VerificationList,
    VerificationSummary,
    VerificationView,
)
from clinical_documentation_verification.application.commands import (
    ApproveVerification,
    OrderValidationResult,
    ParseSig,
    RejectVerification,
    SubmitVerification,
    ValidateOrders,
)
from clinical_documentation_verification.domain.errors import VerificationError
from clinical_documentation_verification.domain.models import MedicationSig, VerificationStatus
from clinical_documentation_verification.runtime.container import Container
from clinical_documentation_verification.security.principals import Principal

router = APIRouter()
VERIFICATION_ID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def container(request: Request) -> Container:
    value: Container = request.app.state.container
    return value


async def caller(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> Principal:
    return await container(request).authenticator.authenticate(authorization)


Deps = Annotated[Container, Depends(container)]
Caller = Annotated[Principal, Depends(caller)]
VerificationId = Annotated[str, Path(pattern=VERIFICATION_ID)]


# -------------------------------------------------------------- verifications


@router.post("/v1/verifications", status_code=201, tags=["verification"])
async def submit_verification(
    *,
    body: SubmitVerification,
    c: Deps,
    who: Caller,
    idempotency_key: Annotated[
        str | None, Header(min_length=8, max_length=128, pattern=r"^[\x21-\x7e]+$")
    ] = None,
) -> VerificationView:
    """Run the full verification workflow; the result always awaits clinician sign-off."""
    verification = await c.service.submit(body, who, idempotency_key=idempotency_key)
    return VerificationView.of(verification)


@router.get("/v1/verifications", tags=["verification"])
async def list_verifications(
    *,
    c: Deps,
    who: Caller,
    patient_id: Annotated[str | None, Query(pattern=r"^[A-Za-z0-9\-.]{1,64}$")] = None,
    status: VerificationStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> VerificationList:
    items = await c.service.list(
        who, patient_id=patient_id, status=status, limit=limit, offset=offset
    )
    return VerificationList(
        items=[VerificationSummary.of(v) for v in items], limit=limit, offset=offset
    )


@router.get("/v1/verifications/{verification_id}", tags=["verification"])
async def get_verification(
    verification_id: VerificationId, c: Deps, who: Caller
) -> VerificationView:
    return VerificationView.of(await c.service.get(verification_id, who))


@router.post("/v1/verifications/{verification_id}/approve", tags=["sign-off"])
async def approve_verification(
    verification_id: VerificationId, body: ApproveVerification, c: Deps, who: Caller
) -> VerificationView:
    """Clinician sign-off; writes the signed DocumentReference and Provenance to the chart."""
    return VerificationView.of(await c.service.approve(verification_id, body, who))


@router.post("/v1/verifications/{verification_id}/reject", tags=["sign-off"])
async def reject_verification(
    verification_id: VerificationId, body: RejectVerification, c: Deps, who: Caller
) -> VerificationView:
    return VerificationView.of(await c.service.reject(verification_id, body, who))


# ---------------------------------------------------------------- order checks


@router.post("/v1/orders/validate", tags=["orders"])
async def validate_orders(body: ValidateOrders, c: Deps, who: Caller) -> OrderValidationResult:
    return await c.service.validate_orders(body, who)


@router.post("/v1/medications/parse-sig", tags=["orders"])
async def parse_sig(body: ParseSig, c: Deps, who: Caller) -> MedicationSig:
    return await c.service.parse(body.text, who)


# ------------------------------------------------------------------------ A2A


@router.get("/.well-known/agent-card.json", tags=["a2a"])
async def agent_card(c: Deps) -> dict[str, object]:
    return a2a.agent_card(c.settings)


JSON_RPC_REQUEST = {
    "type": "object",
    "required": ["jsonrpc", "method"],
    "properties": {
        "jsonrpc": {"type": "string", "enum": ["2.0"]},
        "id": {"oneOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]},
        "method": {"type": "string", "enum": ["message/send", "tasks/get", "tasks/cancel"]},
        "params": {"type": "object"},
    },
}


@router.post(
    "/a2a",
    tags=["a2a"],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": JSON_RPC_REQUEST}},
        }
    },
)
async def a2a_rpc(request: Request, c: Deps, who: Caller) -> JSONResponse:
    """A2A JSON-RPC 2.0: message/send, tasks/get, tasks/cancel."""
    try:
        payload = json.loads(await request.body())
    except ValueError:
        c.metrics.a2a.labels("parse", "error").inc()
        return JSONResponse(a2a.error_response(None, a2a.PARSE_ERROR, "parse error"))
    try:
        response, method = await a2a.handle(payload, service=c.service, principal=who)
    except VerificationError:
        c.metrics.a2a.labels("dispatch", "failure").inc()
        raise
    c.metrics.a2a.labels(method, "error" if "error" in response else "success").inc()
    return JSONResponse(response)


# ------------------------------------------------------------------ operations


@router.get("/healthz", tags=["operations"], include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", tags=["operations"], include_in_schema=False)
async def readyz(c: Deps, response: Response) -> dict[str, str]:
    try:
        async with c.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        response.status_code = 503
        return {"status": "database unavailable"}
    return {
        "status": "ready",
        "extraction": "model+rules" if c.service.model_extraction_enabled else "rules",
    }


@router.get("/metrics", tags=["operations"], include_in_schema=False)
async def metrics(c: Deps) -> Response:
    return Response(generate_latest(c.metrics.registry), media_type=CONTENT_TYPE_LATEST)
