"""Caller authentication: clinicians (enterprise OIDC) and agents (identity fabric credentials).

Routing is by the token's issuer claim, read unverified only to pick the
verifier; every token is then fully verified:

- clinician issuer          -> signature, issuer, audience, expiry against the IdP JWKS;
                               roles and the SMART ``fhirUser`` claim become attributes
- agent, fabric configured  -> RFC 7662 introspection at the fabric (epoch and revocation aware)
- agent, no fabric URL      -> local verification against the fabric JWKS
                               (issuer and audience pinned)
- anything else             -> rejected; there is no anonymous or allow-all mode
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from clinical_documentation_verification.domain.errors import UnauthorizedError
from clinical_documentation_verification.integrations.identity import IdentityFabricClient
from clinical_documentation_verification.security.jwks import KeyResolver, verify_jwt

_FHIR_USER = re.compile(
    r"(?:^|/)(?P<type>Practitioner|PractitionerRole|Patient|RelatedPerson|Person)/(?P<id>[A-Za-z0-9\-.]{1,64})$"
)


def routing_issuer(token: str) -> str | None:
    """The ``iss`` claim of a JWT, read without verification and used only to pick a verifier.

    Returns None for anything that is not a three-part JWT with a JSON payload
    (for example an opaque fabric credential, which only introspection can resolve).
    The selected verifier then checks signature, issuer, audience and expiry.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, binascii.Error):
        return None
    issuer = payload.get("iss") if isinstance(payload, dict) else None
    return issuer if isinstance(issuer, str) else None


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject: str
    kind: Literal["user", "agent"]
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    fhir_user: str | None = Field(
        default=None, description="Normalized FHIR reference, e.g. Practitioner/123"
    )
    delegated: bool = False
    via: Literal["oidc", "fabric", "jwks"]
    token: str = Field(repr=False, exclude=True)

    @property
    def fhir_user_type(self) -> str | None:
        return self.fhir_user.split("/", 1)[0] if self.fhir_user else None


def normalize_fhir_user(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = _FHIR_USER.search(value.strip())
    return f"{match.group('type')}/{match.group('id')}" if match else None


@dataclass(frozen=True)
class ClinicianVerifier:
    resolver: KeyResolver
    issuer: str
    audience: str
    roles_claim: str = "roles"
    fhir_user_claim: str = "fhirUser"


@dataclass(frozen=True)
class AgentJwtVerifier:
    resolver: KeyResolver
    issuer: str
    audience: str


class Authenticator:
    def __init__(
        self,
        clinician: ClinicianVerifier,
        *,
        fabric: IdentityFabricClient | None = None,
        agents: AgentJwtVerifier | None = None,
    ) -> None:
        self._clinician = clinician
        self._fabric = fabric
        self._agents = agents

    async def authenticate(self, authorization: str | None) -> Principal:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise UnauthorizedError("bearer token required")
        token = authorization[7:].strip()
        issuer = routing_issuer(token)
        if issuer is None and self._fabric is None:
            raise UnauthorizedError("malformed token")
        if issuer == self._clinician.issuer:
            return await self._clinician_principal(token)
        if self._fabric is not None:
            return await self._introspected(token)
        if self._agents is not None and issuer == self._agents.issuer:
            return await self._local_agent(token)
        raise UnauthorizedError("token issuer is not trusted")

    async def _clinician_principal(self, token: str) -> Principal:
        v = self._clinician
        claims = await verify_jwt(token, v.resolver, issuer=v.issuer, audience=v.audience)
        raw_roles = claims.get(v.roles_claim, [])
        roles = (
            frozenset(r for r in raw_roles if isinstance(r, str))
            if isinstance(raw_roles, list)
            else frozenset()
        )
        return Principal(
            subject=str(claims["sub"]),
            kind="user",
            roles=roles,
            fhir_user=normalize_fhir_user(claims.get(v.fhir_user_claim)),
            via="oidc",
            token=token,
        )

    async def _introspected(self, token: str) -> Principal:
        if self._fabric is None:  # pragma: no cover - guarded by caller
            raise UnauthorizedError("identity fabric not configured")
        result = await self._fabric.introspect(token)
        if not result.active or not result.sub:
            raise UnauthorizedError("agent credential is not active")
        return Principal(
            subject=result.sub,
            kind="agent",
            scopes=frozenset((result.scope or "").split()),
            delegated=result.act is not None,
            via="fabric",
            token=token,
        )

    async def _local_agent(self, token: str) -> Principal:
        if self._agents is None:  # pragma: no cover - guarded by caller
            raise UnauthorizedError("agent verification not configured")
        v = self._agents
        claims = await verify_jwt(
            token, v.resolver, issuer=v.issuer, audience=v.audience, algorithms=("EdDSA",)
        )
        scope = claims.get("scope", "")
        return Principal(
            subject=str(claims["sub"]),
            kind="agent",
            scopes=frozenset(scope.split()) if isinstance(scope, str) else frozenset(),
            delegated="act" in claims,
            via="jwks",
            token=token,
        )
