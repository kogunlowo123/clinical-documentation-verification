"""Agent2Agent (A2A) protocol surface: Agent Card and JSON-RPC 2.0 endpoint.

Other agents (an ambient scribe, a care-coordination agent) hand this agent
an encounter transcript with ``message/send``. The verification runs
synchronously and comes back as an A2A Task: ``input-required`` while it
waits for clinician sign-off, ``completed`` once the signed note is in the
chart, ``rejected`` if the clinician rejected it. ``tasks/get`` polls a task;
``tasks/cancel`` is refused because only a clinician may close a
verification. Callers authenticate at the HTTP layer with an identity
fabric credential (securitySchemes in the card).
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import ValidationError

from clinical_documentation_verification import __version__
from clinical_documentation_verification.api.schemas import VerificationSummary
from clinical_documentation_verification.application.commands import SubmitVerification
from clinical_documentation_verification.application.service import VerificationService
from clinical_documentation_verification.config import Settings
from clinical_documentation_verification.domain.errors import NotFoundError
from clinical_documentation_verification.domain.models import Verification, VerificationStatus
from clinical_documentation_verification.security.principals import Principal

PROTOCOL_VERSION = "0.3.0"
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002

_STATE = {
    VerificationStatus.PENDING_APPROVAL: "input-required",
    VerificationStatus.APPROVED: "working",
    VerificationStatus.WRITTEN: "completed",
    VerificationStatus.REJECTED: "rejected",
}


def agent_card(settings: Settings) -> dict[str, Any]:
    base = settings.public_url_str
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": "Clinical Documentation Verification Agent",
        "description": (
            "Verifies encounter documentation: segments the transcript by speaker, extracts "
            "problems, medications, orders and allergies, reconciles them with the FHIR chart, "
            "checks orders for dose, route, duplication, interaction and allergy safety, scores "
            "consistency and writes a DocumentReference with Provenance only after a clinician "
            "signs."
        ),
        "url": f"{base}/a2a",
        "preferredTransport": "JSONRPC",
        "version": __version__,
        "provider": {
            "organization": "kogunlowo123",
            "url": "https://github.com/kogunlowo123/clinical-documentation-verification",
        },
        "documentationUrl": "https://github.com/kogunlowo123/clinical-documentation-verification",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": [
            {
                "id": "verify-clinical-documentation",
                "name": "Verify clinical documentation",
                "description": (
                    "Verify an encounter transcript against the patient's FHIR chart and prepare "
                    "a draft note for clinician sign-off. Send a data part with patient_id, "
                    "encounter_id and transcript ({text} or {utterances}); or a text part with the "
                    "transcript and patient_id/encounter_id in message metadata."
                ),
                "tags": [
                    "clinical-documentation",
                    "fhir",
                    "medication-safety",
                    "human-in-the-loop",
                ],
                "examples": [
                    "Verify the transcript of encounter enc-1 for patient pat-1 before sign-off."
                ],
                "inputModes": ["application/json", "text/plain"],
                "outputModes": ["application/json", "text/plain"],
            }
        ],
        "securitySchemes": {
            "agentIdentityFabric": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "Short-lived agent credential issued by the agent identity fabric",
            },
            "clinicianOidc": {
                "type": "openIdConnect",
                "openIdConnectUrl": (
                    f"{settings.clinician_issuer.rstrip('/')}/.well-known/openid-configuration"
                ),
            },
        },
        "security": [{"agentIdentityFabric": []}, {"clinicianOidc": []}],
    }


def task(verification: Verification) -> dict[str, Any]:
    summary = VerificationSummary.of(verification).model_dump(mode="json")
    state = _STATE[verification.status]
    text = (
        f"Consistency {verification.consistency.score:.2f} "
        f"({verification.consistency.band.value}); "
        f"{verification.consistency.critical_findings} critical finding(s). "
        + (
            "Awaiting clinician sign-off."
            if state == "input-required"
            else f"Status: {verification.status.value}."
        )
    )
    return {
        "kind": "task",
        "id": verification.id,
        "contextId": verification.encounter_id,
        "status": {
            "state": state,
            "timestamp": verification.updated_at.isoformat(),
            "message": {
                "kind": "message",
                "role": "agent",
                "messageId": str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"{verification.id}/{verification.version}")
                ),
                "taskId": verification.id,
                "contextId": verification.encounter_id,
                "parts": [{"kind": "text", "text": text}],
            },
        },
        "artifacts": [
            {
                "artifactId": f"{verification.id}-summary",
                "name": "verification-summary",
                "parts": [
                    {
                        "kind": "data",
                        "data": {
                            **summary,
                            "rationale": list(verification.consistency.rationale),
                            "findings": [f.model_dump(mode="json") for f in verification.findings],
                        },
                    }
                ],
            },
            {
                "artifactId": f"{verification.id}-draft-note",
                "name": "draft-note",
                "parts": [{"kind": "text", "text": verification.draft_note}],
            },
        ],
        "metadata": {"band": verification.consistency.band.value},
    }


def error_response(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _submission(params: dict[str, Any]) -> tuple[SubmitVerification, str | None]:
    message = params.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        raise ValueError("params.message must be a user message")
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ValueError("message.parts must be a non-empty list")
    metadata = {**_object(params.get("metadata")), **_object(message.get("metadata"))}
    data = next(
        (
            p["data"]
            for p in parts
            if isinstance(p, dict) and p.get("kind") == "data" and isinstance(p.get("data"), dict)
        ),
        None,
    )
    if data is not None:
        payload = dict(data)
        if isinstance(payload.get("transcript"), str):
            payload["transcript"] = {"text": payload["transcript"]}
    else:
        text = "\n".join(
            str(p.get("text", "")) for p in parts if isinstance(p, dict) and p.get("kind") == "text"
        ).strip()
        if not text:
            raise ValueError("message needs a data part or a text part with the transcript")
        payload = {
            "patient_id": metadata.get("patient_id"),
            "encounter_id": metadata.get("encounter_id"),
            "transcript": {"text": text},
        }
        if metadata.get("note_type"):
            payload["note_type"] = metadata["note_type"]
    message_id = message.get("messageId")
    key = f"a2a:{message_id}"[:128] if isinstance(message_id, str) and message_id else None
    return SubmitVerification.model_validate(payload), key


async def handle(
    body: Any, *, service: VerificationService, principal: Principal
) -> tuple[dict[str, Any], str]:
    """Dispatch one JSON-RPC request. Returns the response and the method for metrics."""
    if (
        not isinstance(body, dict)
        or body.get("jsonrpc") != "2.0"
        or not isinstance(body.get("method"), str)
    ):
        request_id = body.get("id") if isinstance(body, dict) else None
        return (
            error_response(request_id, INVALID_REQUEST, "invalid JSON-RPC 2.0 request"),
            "invalid",
        )
    request_id = body.get("id")
    method: str = body["method"]
    params = _object(body.get("params"))
    if method == "message/send":
        return await _message_send(request_id, params, service, principal), method
    if method in {"tasks/get", "tasks/cancel"}:
        return await _task_method(request_id, method, params, service, principal), method
    return error_response(request_id, METHOD_NOT_FOUND, f"method {method!r} not found"), "unknown"


async def _message_send(
    request_id: Any, params: dict[str, Any], service: VerificationService, principal: Principal
) -> dict[str, Any]:
    try:
        command, key = _submission(params)
    except (ValueError, ValidationError) as exc:
        detail = exc.errors(include_url=False) if isinstance(exc, ValidationError) else str(exc)
        return error_response(
            request_id, INVALID_PARAMS, "invalid message/send params", _jsonable(detail)
        )
    verification = await service.submit(command, principal, idempotency_key=key)
    return _result(request_id, task(verification))


async def _task_method(
    request_id: Any,
    method: str,
    params: dict[str, Any],
    service: VerificationService,
    principal: Principal,
) -> dict[str, Any]:
    task_id = params.get("id")
    if not isinstance(task_id, str) or not task_id:
        return error_response(request_id, INVALID_PARAMS, "params.id is required")
    try:
        verification = await service.get(task_id, principal)
    except NotFoundError:
        return error_response(request_id, TASK_NOT_FOUND, "task not found")
    if method == "tasks/cancel":
        return error_response(
            request_id,
            TASK_NOT_CANCELABLE,
            "verification tasks are closed by clinician approval or rejection only",
        )
    return _result(request_id, task(verification))


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _jsonable(detail: Any) -> Any:
    if isinstance(detail, list):
        return [
            {"loc": [str(p) for p in e.get("loc", ())], "msg": str(e.get("msg", ""))}
            for e in detail
        ]
    return str(detail)
