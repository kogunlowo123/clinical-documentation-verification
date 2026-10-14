"""In-process stand-ins for external systems, speaking their recorded wire formats.

Every value here is synthetic. Patient pat-1001 and the encounter transcripts are
invented for testing and describe no real person.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

FHIR_BASE = "https://fhir.test/r4"
SMART_TOKEN_URL = "https://auth.test/token"
FABRIC_URL = "https://fabric.test"
LLM_URL = "https://llm.test"
CLINICIAN_ISSUER = "https://login.test/tenant"
AGENT_ISSUER = "https://identity.test"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
SNOMED = "http://snomed.info/sct"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"


# ------------------------------------------------------------------------- identity


@dataclass
class FakeIdp:
    """OIDC issuer with an ES256 key (clinician enterprise IdP)."""

    issuer: str
    kid: str = "k1"
    key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )

    def jwks(self) -> dict[str, Any]:
        jwk = jwt.algorithms.ECAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        jwk.update({"kid": self.kid, "alg": "ES256", "use": "sig"})
        return {"keys": [jwk]}

    def mint(self, *, sub: str, aud: str, ttl: int = 300, **extra: Any) -> str:
        now = int(time.time())
        claims = {"iss": self.issuer, "sub": sub, "aud": aud, "iat": now, "exp": now + ttl}
        claims.update(extra)
        return jwt.encode(claims, self.key, algorithm="ES256", headers={"kid": self.kid})


@dataclass
class FakeAgentIssuer:
    """The identity fabric's EdDSA signing key, for locally verified agent credentials."""

    issuer: str = AGENT_ISSUER
    kid: str = "2026-10"
    key: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)

    def jwks(self) -> dict[str, Any]:
        jwk = jwt.algorithms.OKPAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        jwk.update({"kid": self.kid, "alg": "EdDSA", "use": "sig"})
        return {"keys": [jwk]}

    def mint(
        self,
        name: str = "ambient-scribe",
        *,
        scope: str = "documentation:verify documentation:read",
        ttl: int = 600,
        **extra: Any,
    ) -> str:
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "sub": f"spiffe://agents.test/agent/clinical/{name}",
            "aud": "agent-platform",
            "iat": now,
            "exp": now + ttl,
            "scope": scope,
            "jti": f"jti-{name}-{now}",
        }
        claims.update(extra)
        return jwt.encode(claims, self.key, algorithm="EdDSA", headers={"kid": self.kid})


@dataclass
class FakeClock:
    current: datetime = field(default_factory=lambda: datetime.now(UTC).replace(microsecond=0))

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


# ---------------------------------------------------------------------------- FHIR


def condition(
    cid: str, display: str, snomed: str | None, icd10: str | None, status: str
) -> dict[str, Any]:
    coding = []
    if snomed:
        coding.append({"system": SNOMED, "code": snomed, "display": display})
    if icd10:
        coding.append({"system": ICD10, "code": icd10, "display": display})
    return {
        "resourceType": "Condition",
        "id": cid,
        "meta": {"versionId": "1"},
        "clinicalStatus": {"coding": [{"code": status}]},
        "verificationStatus": {"coding": [{"code": "confirmed"}]},
        "code": {"coding": coding, "text": display},
        "subject": {"reference": "Patient/pat-1001"},
    }


def medication_request(
    mid: str,
    text: str,
    rxnorm: str | None,
    *,
    dose: float | None = None,
    unit: str = "mg",
    route: str | None = None,
    per_day: int | None = None,
    status: str = "active",
) -> dict[str, Any]:
    dosage: dict[str, Any] = {"text": text}
    if dose is not None:
        dosage["doseAndRate"] = [{"doseQuantity": {"value": dose, "unit": unit, "code": unit}}]
    if route is not None:
        dosage["route"] = {"coding": [{"system": SNOMED, "code": route}]}
    if per_day is not None:
        dosage["timing"] = {"repeat": {"frequency": per_day, "period": 1, "periodUnit": "d"}}
    concept: dict[str, Any] = {"text": text.split(" ", maxsplit=1)[0]}
    if rxnorm:
        concept["coding"] = [{"system": RXNORM, "code": rxnorm}]
    return {
        "resourceType": "MedicationRequest",
        "id": mid,
        "meta": {"versionId": "2"},
        "status": status,
        "intent": "order",
        "medicationCodeableConcept": concept,
        "subject": {"reference": "Patient/pat-1001"},
        "dosageInstruction": [dosage],
    }


def allergy(aid: str, substance: str, *, status: str = "active") -> dict[str, Any]:
    return {
        "resourceType": "AllergyIntolerance",
        "id": aid,
        "meta": {"versionId": "1"},
        "clinicalStatus": {"coding": [{"code": status}]},
        "verificationStatus": {"coding": [{"code": "confirmed"}]},
        "code": {"text": substance},
        "criticality": "high",
        "patient": {"reference": "Patient/pat-1001"},
    }


def synthetic_chart() -> dict[str, dict[str, dict[str, Any]]]:
    """Chart of synthetic patient pat-1001."""
    return {
        "Patient": {
            "pat-1001": {
                "resourceType": "Patient",
                "id": "pat-1001",
                "name": [{"family": "Testpatient", "given": ["Alex"]}],
                "birthDate": "1961-04-12",
            }
        },
        "Condition": {
            c["id"]: c
            for c in (
                condition("cond-htn", "Essential hypertension", "38341003", "I10", "active"),
                condition("cond-dm2", "Type 2 diabetes mellitus", None, "E11.9", "active"),
                condition("cond-asthma", "Asthma", "195967001", "J45.909", "resolved"),
            )
        },
        "MedicationRequest": {
            m["id"]: m
            for m in (
                medication_request(
                    "mr-lis",
                    "lisinopril 10 mg tablet daily",
                    "29046",
                    dose=10,
                    route="26643006",
                    per_day=1,
                ),
                medication_request("mr-ator", "atorvastatin 40 mg by mouth at bedtime", "83367"),
                medication_request("mr-asa", "aspirin 81 mg PO daily", "1191"),
                medication_request("mr-old", "metoprolol 25 mg PO BID", "6918", status="stopped"),
            )
        },
        "AllergyIntolerance": {a["id"]: a for a in (allergy("alg-pcn", "Penicillin"),)},
    }


@dataclass
class FakeFhirServer:
    """FHIR R4 server: read, searchset paging, transaction with conditional create."""

    base: str = FHIR_BASE
    resources: dict[str, dict[str, dict[str, Any]]] = field(default_factory=synthetic_chart)
    page_size: int = 2
    require_token: str | None = None
    fail_gets: int = 0
    fail_transactions: int = 0
    transaction_status: int = 503
    next_link_override: str | None = None
    transactions: list[dict[str, Any]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    created: int = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if (
            self.require_token
            and request.headers.get("authorization") != f"Bearer {self.require_token}"
        ):
            return httpx.Response(401, json=_outcome("login", "invalid or missing token"))
        path = urlsplit(str(request.url)).path.removeprefix(urlsplit(self.base).path).strip("/")
        if request.method == "GET":
            return self._get(path, request)
        if request.method == "POST" and path == "":
            return self._transaction(json.loads(request.content))
        return httpx.Response(405, json=_outcome("not-supported", "method not allowed"))

    def _get(self, path: str, request: httpx.Request) -> httpx.Response:
        if self.fail_gets > 0:
            self.fail_gets -= 1
            return httpx.Response(503, json=_outcome("transient", "maintenance"))
        parts = path.split("/")
        if len(parts) == 2:
            found = self.resources.get(parts[0], {}).get(parts[1])
            if found is None:
                return httpx.Response(404, json=_outcome("not-found", "unknown resource"))
            return httpx.Response(200, json=found)
        return self._search(parts[0], parse_qs(urlsplit(str(request.url)).query))

    def _search(self, resource_type: str, query: dict[str, list[str]]) -> httpx.Response:
        patient = query.get("patient", [""])[0]
        statuses = set(query.get("status", [""])[0].split(",")) - {""}
        offset = int(query.get("_offset", ["0"])[0])
        matches = [
            r
            for r in self.resources.get(resource_type, {}).values()
            if (r.get("subject") or r.get("patient") or {}).get("reference") == f"Patient/{patient}"
            and (not statuses or r.get("status") in statuses)
        ]
        page = matches[offset : offset + self.page_size]
        bundle: dict[str, Any] = {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": len(matches),
            "entry": [{"resource": r, "search": {"mode": "match"}} for r in page],
            "link": [],
        }
        if offset + self.page_size < len(matches):
            status_q = f"&status={','.join(sorted(statuses))}" if statuses else ""
            bundle["link"].append(
                {
                    "relation": "next",
                    "url": self.next_link_override
                    or f"{self.base}/{resource_type}?patient={patient}{status_q}"
                    f"&_offset={offset + self.page_size}",
                }
            )
        return httpx.Response(200, json=bundle)

    def _transaction(self, bundle: dict[str, Any]) -> httpx.Response:
        if bundle.get("type") != "transaction":
            return httpx.Response(400, json=_outcome("invalid", "expected a transaction bundle"))
        if self.fail_transactions > 0:
            self.fail_transactions -= 1
            return httpx.Response(
                self.transaction_status, json=_outcome("exception", "write failed")
            )
        self.transactions.append(bundle)
        responses = []
        for entry in bundle["entry"]:
            resource = entry["resource"]
            kind = resource["resourceType"]
            condition_query = entry["request"].get("ifNoneExist")
            existing = None
            if condition_query:
                value = condition_query.split("|", 1)[1]
                existing = next(
                    (
                        rid
                        for rid, r in self.resources.get(kind, {}).items()
                        if any(i.get("value") == value for i in r.get("identifier", []))
                    ),
                    None,
                )
            if existing:
                responses.append(
                    {"response": {"status": "200 OK", "location": f"{kind}/{existing}/_history/1"}}
                )
                continue
            self.created += 1
            rid = f"{kind.lower()}-{self.created}"
            self.resources.setdefault(kind, {})[rid] = {**resource, "id": rid}
            responses.append(
                {"response": {"status": "201 Created", "location": f"{kind}/{rid}/_history/1"}}
            )
        return httpx.Response(
            200, json={"resourceType": "Bundle", "type": "transaction-response", "entry": responses}
        )

    def written(self, kind: str) -> list[dict[str, Any]]:
        return [
            r for rid, r in self.resources.get(kind, {}).items() if rid.startswith(kind.lower())
        ]


def _outcome(code: str, text: str) -> dict[str, Any]:
    return {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": code, "diagnostics": text}],
    }


# ---------------------------------------------------------------- SMART token server


@dataclass
class FakeSmartServer:
    client_id: str = "cdv-backend"
    public_key: Any = None
    issued: int = 0
    fail: bool = False
    last_assertion: dict[str, Any] | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(503)
        form = parse_qs(request.content.decode())
        if form.get("grant_type") != ["client_credentials"]:
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        assertion = form["client_assertion"][0]
        try:
            claims = jwt.decode(
                assertion,
                self.public_key,
                algorithms=["ES384", "RS384"],
                audience=SMART_TOKEN_URL,
                leeway=900,
            )
        except jwt.PyJWTError:
            return httpx.Response(401, json={"error": "invalid_client"})
        if claims["iss"] != self.client_id or claims["sub"] != self.client_id:
            return httpx.Response(401, json={"error": "invalid_client"})
        self.last_assertion = claims
        self.issued += 1
        return httpx.Response(
            200,
            json={
                "access_token": f"fhir-at-{self.issued}",
                "token_type": "bearer",
                "expires_in": 300,
            },
        )


# ----------------------------------------------------------------- identity fabric


@dataclass
class FakeFabric:
    """agent-identity-fabric wire format: /oauth2/token, /oauth2/introspect, /v1/authorize."""

    active: dict[str, dict[str, Any]] = field(default_factory=dict)
    decide: Callable[[str, dict[str, Any]], str] = lambda _sub, _body: "allow"
    authorize_calls: list[dict[str, Any]] = field(default_factory=list)
    tokens_issued: int = 0
    down: bool = False

    def credential(self, sub: str, scope: str, *, act: dict[str, Any] | None = None) -> str:
        token = f"fabric-token-{len(self.active) + 1}"
        self.active[token] = {"sub": sub, "scope": scope, "act": act}
        return token

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503)
        routes = {
            "/oauth2/introspect": self._introspect,
            "/v1/authorize": self._authorize,
            "/oauth2/token": self._token,
        }
        route = routes.get(request.url.path)
        return route(request) if route else httpx.Response(404)

    def _introspect(self, request: httpx.Request) -> httpx.Response:
        token = parse_qs(request.content.decode()).get("token", [""])[0]
        info = self.active.get(token)
        if info is None:
            return httpx.Response(200, json={"active": False})
        body = {
            "active": True,
            "sub": info["sub"],
            "scope": info["scope"],
            "exp": int(time.time()) + 600,
        }
        if info.get("act"):
            body["act"] = info["act"]
        return httpx.Response(200, json=body)

    def _authorize(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "")[7:]
        info = self.active.get(token)
        if info is None:
            return httpx.Response(
                401, json={"error": "unauthorized", "error_description": "bad token"}
            )
        body = json.loads(request.content)
        self.authorize_calls.append(body)
        decision = self.decide(info["sub"], body)
        return httpx.Response(
            200,
            json={
                "decision": decision,
                "agent": info["sub"],
                "action": body["action"],
                "resource": f"{body['resource_type']}:{body['resource_id']}",
                "matched_rules": (
                    ["allow-scoped-actions"] if decision == "allow" else ["fabric-rule"]
                ),
                "policy_version": "2026.10.0",
                "policy_digest": "sha256:" + "f" * 64,
                "reason": f"fabric {decision}",
                "delegation_chain": [],
            },
        )

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        if form.get("assertion", [""])[0] != "projected-sa-token":
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.tokens_issued += 1
        token = self.credential(form["client_id"][0], "fhir:write:DocumentReference")
        return httpx.Response(
            200,
            json={
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": 600,
                "scope": "fhir:write:DocumentReference",
            },
        )


# --------------------------------------------------------------------------- model


@dataclass
class FakeModel:
    """OpenAI-compatible chat completions endpoint returning queued answers."""

    answers: list[str | int] = field(default_factory=list)
    prompts: list[dict[str, Any]] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.prompts.append(json.loads(request.content))
        answer = self.answers.pop(0) if self.answers else 500
        if isinstance(answer, int):
            return httpx.Response(answer, json={"error": {"message": "upstream failure"}})
        return httpx.Response(
            200,
            json={
                "id": "cmpl-1",
                "model": "clinical-extractor",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": answer},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 900, "completion_tokens": 300},
            },
        )


@dataclass
class Upstreams:
    """Routes outbound HTTP by host to the fake systems."""

    fhir: FakeFhirServer = field(default_factory=FakeFhirServer)
    smart: FakeSmartServer = field(default_factory=FakeSmartServer)
    fabric: FakeFabric = field(default_factory=FakeFabric)

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "fhir.test":
            return self.fhir.handle(request)
        if host == "auth.test":
            return self.smart.handle(request)
        if host == "fabric.test":
            return self.fabric.handle(request)
        return httpx.Response(404, json={"error": "unknown host"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
