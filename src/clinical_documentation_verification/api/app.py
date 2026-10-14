"""FastAPI application factory: REST API, A2A endpoint and the mounted MCP server."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from agentic_core.observability import bind_correlation_id, get_logger
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings

from clinical_documentation_verification import __version__
from clinical_documentation_verification.api.mcp_server import build_mcp
from clinical_documentation_verification.api.routes import router
from clinical_documentation_verification.domain.errors import VerificationError
from clinical_documentation_verification.runtime.container import Container

ContainerFactory = Callable[[], Awaitable[Container]]

log = get_logger("clinical_documentation_verification.api")


def create_app(factory: ContainerFactory) -> FastAPI:
    holder: dict[str, Container] = {}

    def current() -> Container:
        return holder["container"]

    mcp = build_mcp(current)
    # Host-header (DNS rebinding) protection is the ingress's job in Kubernetes; the
    # service binds to the pod network only and every tool call is authenticated.
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = await factory()
        holder["container"] = container
        app.state.container = container
        log.info(
            "startup",
            version=__version__,
            model_extraction=container.service.model_extraction_enabled,
        )
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await container.aclose()

    app = FastAPI(
        title="Clinical Documentation Verification",
        version=__version__,
        description=(
            "Agentic verification of clinical encounter documentation: transcript segmentation, "
            "structured extraction, FHIR chart reconciliation, order safety validation, "
            "consistency scoring and clinician-approved FHIR write-back."
        ),
        lifespan=lifespan,
    )
    app.include_router(router)
    app.mount("/mcp", mcp_app)

    @app.middleware("http")
    async def correlation_and_metrics(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-correlation-id")
        cid = incoming if incoming and len(incoming) <= 64 and incoming.isprintable() else None
        started = time.perf_counter()
        with bind_correlation_id(cid) as bound:
            response = await call_next(request)
        response.headers["X-Correlation-ID"] = bound
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        route = request.scope.get("route")
        path = getattr(
            route, "path", "/mcp" if request.url.path.startswith("/mcp") else "unmatched"
        )
        container: Container | None = getattr(request.app.state, "container", None)
        if container is not None:
            container.metrics.requests.labels(
                request.method, path, str(response.status_code)
            ).observe(time.perf_counter() - started)
        return response

    @app.exception_handler(VerificationError)
    async def domain_error(_: Request, exc: VerificationError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(
            status_code=exc.status,
            content={"error": exc.code, "error_description": exc.message},
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_request",
                "error_description": "request failed validation",
                "details": [
                    {"loc": [str(p) for p in e.get("loc", ())], "msg": str(e.get("msg", ""))}
                    for e in exc.errors()
                ],
            },
        )

    return app
