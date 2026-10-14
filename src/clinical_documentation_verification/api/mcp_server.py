"""MCP server (official ``mcp`` SDK, streamable HTTP) exposing the verification tools.

Tools mirror the REST use cases and share their authorization: each call
authenticates the caller from the HTTP ``Authorization`` header, the
application service evaluates policy for the action, and an audit event is
written per call. Listing tools needs no credential; calling one does.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agentic_core.observability import correlation_id
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from clinical_documentation_verification import __version__
from clinical_documentation_verification.api.schemas import VerificationSummary
from clinical_documentation_verification.application.commands import (
    OrderValidationResult,
    ParseSig,
    SubmitVerification,
    ValidateOrders,
)
from clinical_documentation_verification.domain.errors import VerificationError
from clinical_documentation_verification.domain.models import (
    DiffEntry,
    Finding,
    MedicationSig,
)
from clinical_documentation_verification.runtime.container import Container
from clinical_documentation_verification.security.principals import Principal

ContainerProvider = Callable[[], Container]


class VerificationResult(BaseModel):
    summary: VerificationSummary
    findings: list[Finding]
    chart_diff: list[DiffEntry]
    rationale: list[str]
    draft_note: str
    awaiting_clinician_signoff: bool = Field(
        description="True until a clinician approves; nothing is written to the chart before that"
    )


class GetVerification(BaseModel):
    verification_id: str = Field(pattern=r"^[0-9a-f\-]{36}$")


async def _call[T](
    container: ContainerProvider,
    ctx: Context,
    tool: str,
    work: Callable[[Container, Principal], Awaitable[T]],
) -> T:
    c = container()
    headers = ctx.headers or {}
    try:
        principal = await c.authenticator.authenticate(headers.get("authorization"))
    except VerificationError as exc:
        c.metrics.tool_calls.labels(tool, "unauthenticated").inc()
        raise ToolError(f"{exc.code}: {exc.message}") from exc
    try:
        result = await work(c, principal)
    except VerificationError as exc:
        c.metrics.tool_calls.labels(tool, exc.code).inc()
        await _audit(c, principal, tool, "failure", {"error": exc.code})
        raise ToolError(f"{exc.code}: {exc.message}") from exc
    c.metrics.tool_calls.labels(tool, "success").inc()
    await _audit(c, principal, tool, "success", {})
    return result


async def _audit(
    c: Container, principal: Principal, tool: str, outcome: str, details: dict[str, Any]
) -> None:
    await c.audit.record(
        actor=principal.subject,
        action=f"mcp.tool.{tool}",
        resource=f"mcp:{tool}",
        outcome=outcome,
        correlation_id=correlation_id(),
        details={"via": principal.via, **details},
    )


def build_mcp(container: ContainerProvider) -> MCPServer:
    server: MCPServer = MCPServer(
        name="clinical-documentation-verification",
        version=__version__,
        instructions=(
            "Verify clinical encounter documentation against the FHIR chart. Verification never "
            "writes to the chart: a clinician must approve through the REST API first."
        ),
    )

    @server.tool(
        name="verify_clinical_documentation",
        description=(
            "Segment an encounter transcript, extract problems, medications, orders and allergies, "
            "reconcile with the patient's FHIR chart, validate orders and score consistency. "
            "Returns a draft note awaiting clinician sign-off."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False
        ),
    )
    async def verify_clinical_documentation(
        request: SubmitVerification, ctx: Context
    ) -> VerificationResult:
        async def work(c: Container, p: Principal) -> VerificationResult:
            v = await c.service.submit(request, p)
            return VerificationResult(
                summary=VerificationSummary.of(v),
                findings=list(v.findings),
                chart_diff=list(v.diff),
                rationale=list(v.consistency.rationale),
                draft_note=v.draft_note,
                awaiting_clinician_signoff=v.write is None,
            )

        return await _call(container, ctx, "verify_clinical_documentation", work)

    @server.tool(
        name="get_verification",
        description="Fetch the status, consistency score and findings of a verification.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def get_verification(request: GetVerification, ctx: Context) -> VerificationSummary:
        async def work(c: Container, p: Principal) -> VerificationSummary:
            return VerificationSummary.of(await c.service.get(request.verification_id, p))

        return await _call(container, ctx, "get_verification", work)

    @server.tool(
        name="validate_orders",
        description=(
            "Check draft medication orders for dose range, route and form compatibility, "
            "duplicates, "
            "therapeutic duplication, class interactions, allergies and Do-Not-Use abbreviations."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def validate_orders(request: ValidateOrders, ctx: Context) -> OrderValidationResult:
        async def work(c: Container, p: Principal) -> OrderValidationResult:
            return await c.service.validate_orders(request, p)

        return await _call(container, ctx, "validate_orders", work)

    @server.tool(
        name="parse_medication_sig",
        description=(
            "Parse a medication instruction into ingredient, dose, route, form, frequency and PRN."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )
    async def parse_medication_sig(request: ParseSig, ctx: Context) -> MedicationSig:
        async def work(c: Container, p: Principal) -> MedicationSig:
            return await c.service.parse(request.text, p)

        return await _call(container, ctx, "parse_medication_sig", work)

    return server
