"""Typed client for the agent identity fabric (github.com/kogunlowo123/agent-identity-fabric).

- ``token``       RFC 7523 jwt-bearer grant: workload token in, short-lived agent credential out
- ``introspect``  RFC 7662: validate an inbound agent credential (epoch and revocation aware)
- ``authorize``   policy decision for an action on a resource, delegation-chain aware
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from agentic_core.domain.classification import DataClassification, Decision
from pydantic import BaseModel, ConfigDict, ValidationError

from clinical_documentation_verification.domain.errors import (
    UnauthorizedError,
    UpstreamError,
    UpstreamUnavailableError,
)

JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"


class IssuedCredential(BaseModel):
    model_config = ConfigDict(frozen=True)

    access_token: str
    token_type: str
    expires_in: int
    scope: str = ""


class Introspection(BaseModel):
    model_config = ConfigDict(frozen=True)

    active: bool
    sub: str | None = None
    scope: str | None = None
    exp: int | None = None
    act: dict[str, Any] | None = None


class FabricDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: Decision
    agent: str
    action: str
    resource: str
    matched_rules: tuple[str, ...]
    policy_version: str
    policy_digest: str
    reason: str
    delegation_chain: tuple[str, ...] = ()


class IdentityFabricClient:
    def __init__(self, base_url: str, *, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._http = http

    async def _post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._http.post(f"{self._base}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError(
                f"identity fabric unreachable: {type(exc).__name__}"
            ) from exc
        if response.status_code == 401:
            raise UnauthorizedError("identity fabric rejected the credential")
        if response.status_code >= 500:
            raise UpstreamUnavailableError(f"identity fabric returned {response.status_code}")
        if response.status_code >= 400:
            raise UpstreamError(
                f"identity fabric returned {response.status_code}{_error(response)}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise UpstreamError("identity fabric returned a non-JSON body") from exc
        if not isinstance(body, dict):
            raise UpstreamError("identity fabric returned a non-object body")
        return body

    async def token(
        self, *, client_id: str, assertion: str, scope: str | None = None
    ) -> IssuedCredential:
        data = {"grant_type": JWT_BEARER, "client_id": client_id, "assertion": assertion}
        if scope:
            data["scope"] = scope
        return _parse(IssuedCredential, await self._post("/oauth2/token", data=data))

    async def introspect(self, token: str) -> Introspection:
        return _parse(Introspection, await self._post("/oauth2/introspect", data={"token": token}))

    async def authorize(
        self,
        token: str,
        *,
        action: str,
        resource_type: str,
        resource_id: str,
        classification: DataClassification,
        attributes: dict[str, Any],
        environment: dict[str, Any],
    ) -> FabricDecision:
        body = await self._post(
            "/v1/authorize",
            json={
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "resource_classification": int(classification),
                "resource_attributes": attributes,
                "environment": environment,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        return _parse(FabricDecision, body)


def _parse[T: BaseModel](model: type[T], body: dict[str, Any]) -> T:
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        raise UpstreamError(f"identity fabric response is not a valid {model.__name__}") from exc


def _error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return f": {body['error']}"
    return ""


class AgentCredentialSource:
    """This service's own fabric credential, obtained with its projected workload token.

    The workload token file is re-read on every refresh because Kubernetes
    rotates projected service-account tokens.
    """

    def __init__(
        self,
        client: IdentityFabricClient,
        *,
        agent_id: str,
        workload_token_file: Path,
        clock: Callable[[], datetime],
        scope: str | None = None,
    ) -> None:
        self._client = client
        self._agent_id = agent_id
        self._token_file = workload_token_file
        self._clock = clock
        self._scope = scope
        self._credential: str | None = None
        self._expires_at: datetime | None = None
        self._lock = asyncio.Lock()

    @property
    def agent_id(self) -> str:
        return self._agent_id

    async def credential(self) -> str:
        async with self._lock:
            now = self._clock()
            if self._credential is None or self._expires_at is None or now >= self._expires_at:
                assertion = (
                    await asyncio.to_thread(self._token_file.read_text, encoding="utf-8")
                ).strip()
                issued = await self._client.token(
                    client_id=self._agent_id, assertion=assertion, scope=self._scope
                )
                self._credential = issued.access_token
                self._expires_at = now + timedelta(seconds=max(issued.expires_in - 60, 1))
            return self._credential
