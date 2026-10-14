"""Domain errors mapped to HTTP status codes by the API layer."""

from __future__ import annotations


class VerificationError(Exception):
    code = "invalid_request"
    status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(VerificationError):
    code = "not_found"
    status = 404


class ConflictError(VerificationError):
    code = "conflict"
    status = 409


class InvalidStateError(VerificationError):
    code = "invalid_state"
    status = 409


class UnauthorizedError(VerificationError):
    code = "unauthorized"
    status = 401


class ForbiddenError(VerificationError):
    code = "forbidden"
    status = 403


class UpstreamError(VerificationError):
    """An upstream system (FHIR server, identity fabric) rejected a request."""

    code = "upstream_error"
    status = 502


class UpstreamUnavailableError(VerificationError):
    """An upstream system could not be reached or timed out."""

    code = "upstream_unavailable"
    status = 503
