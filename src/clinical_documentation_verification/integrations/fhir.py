"""FHIR R4 client: SMART backend-services auth, paged search, chart snapshot, transactional write.

Wire behaviour follows the FHIR R4 RESTful API (``application/fhir+json``,
searchset Bundles with ``link[relation=next]`` paging, transaction Bundles
with ``ifNoneExist`` conditional create) and SMART App Launch "Backend
Services" (client_credentials grant with a signed JWT client assertion,
RFC 7523). Paging links are followed only on the configured server origin,
so a malicious or misconfigured server cannot redirect the bearer token.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
import jwt

from clinical_documentation_verification.clinical.formulary import Formulary
from clinical_documentation_verification.clinical.sig import parse_frequency, parse_route, parse_sig
from clinical_documentation_verification.domain.errors import (
    NotFoundError,
    UpstreamError,
    UpstreamUnavailableError,
)
from clinical_documentation_verification.domain.models import (
    ChartAllergy,
    ChartCondition,
    ChartMedication,
    ChartSnapshot,
    Coding,
    Dose,
    FhirWriteResult,
    Frequency,
    MedicationSig,
    Route,
)

FHIR_JSON = "application/fhir+json"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
SNOMED = "http://snomed.info/sct"
CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
ROUTE_CODES: dict[str, Route] = {
    "26643006": Route.PO,
    "47625008": Route.IV,
    "78421000": Route.IM,
    "34206005": Route.SC,
    "37839007": Route.SL,
    "37161004": Route.PR,
    "6064005": Route.TOP,
    "45890007": Route.TD,
    "54485002": Route.OPH,
    "10547007": Route.OTIC,
    "46713006": Route.NASAL,
    "127492001": Route.NG,
}
_RETRYABLE = frozenset({502, 503, 504})
_PERIOD_HOURS = {"s": 1 / 3600, "min": 1 / 60, "h": 1.0, "d": 24.0, "wk": 168.0, "mo": 720.0}
_PER_DAY_CODES = {1.0: "DAILY", 2.0: "BID", 3.0: "TID", 4.0: "QID"}


# ------------------------------------------------------------------------ auth


class FhirAuth(Protocol):
    async def headers(self) -> dict[str, str]: ...


class NoAuth:
    """For development servers without authorization (refused in production by Settings)."""

    async def headers(self) -> dict[str, str]:
        return {}


class SmartBackendAuth:
    """SMART Backend Services: client_credentials with a private_key_jwt client assertion."""

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        private_key: Any,
        key_id: str,
        algorithm: Literal["RS384", "ES384"],
        scope: str,
        http: httpx.AsyncClient,
        clock: Callable[[], datetime],
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._key = private_key
        self._kid = key_id
        self._alg = algorithm
        self._scope = scope
        self._http = http
        self._clock = clock
        self._token: str | None = None
        self._expires_at: datetime | None = None
        self._lock = asyncio.Lock()

    def assertion(self) -> str:
        now = self._clock()
        claims = {
            "iss": self._client_id,
            "sub": self._client_id,
            "aud": self._token_url,
            "jti": uuid.uuid4().hex,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        }
        return jwt.encode(
            claims, self._key, algorithm=self._alg, headers={"kid": self._kid, "typ": "JWT"}
        )

    async def headers(self) -> dict[str, str]:
        async with self._lock:
            now = self._clock()
            if self._token is None or self._expires_at is None or now >= self._expires_at:
                await self._fetch(now)
            return {"Authorization": f"Bearer {self._token}"}

    async def _fetch(self, now: datetime) -> None:
        try:
            response = await self._http.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "scope": self._scope,
                    "client_assertion_type": CLIENT_ASSERTION_TYPE,
                    "client_assertion": self.assertion(),
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError("FHIR authorization server unreachable") from exc
        if response.status_code != 200:
            raise UpstreamError(f"FHIR authorization server returned {response.status_code}")
        try:
            body = response.json()
            token = str(body["access_token"])
            lifetime = int(body.get("expires_in", 300))
        except (ValueError, KeyError, TypeError) as exc:
            raise UpstreamError("FHIR authorization server returned a malformed token") from exc
        self._token = token
        # Refresh 30 s early so a token never expires mid-request.
        self._expires_at = now + timedelta(seconds=max(lifetime - 30, 1))


# ---------------------------------------------------------------------- client


class FhirClient:
    def __init__(
        self,
        base_url: str,
        *,
        http: httpx.AsyncClient,
        auth: FhirAuth,
        max_pages: int = 20,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._base = base_url.rstrip("/")
        origin = urlsplit(self._base)
        self._origin = (origin.scheme, origin.netloc)
        self._http = http
        self._auth = auth
        self._max_pages = max_pages
        self._sleep = sleep

    @property
    def base_url(self) -> str:
        return self._base

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {"Accept": FHIR_JSON, **(await self._auth.headers())}
        if "json" in kwargs:
            headers["Content-Type"] = FHIR_JSON
        attempts = 2 if method == "GET" else 1
        for attempt in range(1, attempts + 1):
            try:
                response = await self._http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                if attempt < attempts:
                    await self._sleep(0.2)
                    continue
                raise UpstreamUnavailableError(
                    f"FHIR server unreachable: {type(exc).__name__}"
                ) from exc
            if response.status_code in _RETRYABLE and attempt < attempts:
                await self._sleep(0.2)
                continue
            return self._check(response)
        raise UpstreamUnavailableError(
            "FHIR server unavailable"
        )  # pragma: no cover - loop exits above

    @staticmethod
    def _check(response: httpx.Response) -> httpx.Response:
        status = response.status_code
        if status < 400:
            return response
        if status == 404:
            raise NotFoundError("FHIR resource not found")
        if status in _RETRYABLE or status == 429:
            raise UpstreamUnavailableError(f"FHIR server returned {status}")
        raise UpstreamError(f"FHIR server rejected the request ({status}){_outcome(response)}")

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise UpstreamError("FHIR server returned a non-JSON body") from exc
        if not isinstance(body, dict):
            raise UpstreamError("FHIR server returned a non-object body")
        return body

    def _same_origin(self, url: str) -> bool:
        parts = urlsplit(url)
        return (parts.scheme, parts.netloc) == self._origin

    async def read(self, resource_type: str, resource_id: str) -> dict[str, Any]:
        return self._json(await self._send("GET", f"{self._base}/{resource_type}/{resource_id}"))

    async def search(self, resource_type: str, params: dict[str, str]) -> list[dict[str, Any]]:
        resources: list[dict[str, Any]] = []
        bundle = self._json(await self._send("GET", f"{self._base}/{resource_type}", params=params))
        for page in range(self._max_pages):
            for entry in bundle.get("entry", []) or []:
                resource = entry.get("resource") if isinstance(entry, dict) else None
                search = entry.get("search", {}) if isinstance(entry, dict) else {}
                if (
                    isinstance(resource, dict)
                    and resource.get("resourceType") == resource_type
                    and (not isinstance(search, dict) or search.get("mode", "match") == "match")
                ):
                    resources.append(resource)
            following = next(
                (
                    link.get("url")
                    for link in bundle.get("link", []) or []
                    if isinstance(link, dict) and link.get("relation") == "next"
                ),
                None,
            )
            if not following or page + 1 >= self._max_pages:
                break
            if not self._same_origin(str(following)):
                raise UpstreamError("FHIR paging link points outside the configured server")
            bundle = self._json(await self._send("GET", str(following)))
        return resources

    async def transaction(self, bundle: dict[str, Any]) -> dict[str, Any]:
        return self._json(await self._send("POST", self._base, json=bundle))


def _outcome(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict) and body.get("resourceType") == "OperationOutcome":
        issues = body.get("issue") or []
        texts = [
            str(i.get("diagnostics") or i.get("details", {}).get("text") or i.get("code"))
            for i in issues
            if isinstance(i, dict)
        ]
        return f": {'; '.join(texts)[:300]}" if texts else ""
    return ""


# ------------------------------------------------------------------- mapping


def _codings(concept: Any) -> tuple[Coding, ...]:
    if not isinstance(concept, dict):
        return ()
    return tuple(
        Coding(system=str(c["system"]), code=str(c["code"]), display=c.get("display"))
        for c in concept.get("coding", []) or []
        if isinstance(c, dict) and c.get("system") and c.get("code")
    )


def _text(concept: Any) -> str:
    if not isinstance(concept, dict):
        return ""
    if concept.get("text"):
        return str(concept["text"])
    for coding in concept.get("coding", []) or []:
        if isinstance(coding, dict) and coding.get("display"):
            return str(coding["display"])
    return ""


def _status(concept: Any, default: str) -> str:
    """Code of a status CodeableConcept (fixed FHIR value sets, so the system may be omitted)."""
    if isinstance(concept, dict):
        for coding in concept.get("coding", []) or []:
            if isinstance(coding, dict) and coding.get("code"):
                return str(coding["code"])
    return default


def _reference(resource: dict[str, Any]) -> str:
    return f"{resource.get('resourceType')}/{resource.get('id')}"


def _versioned(resource: dict[str, Any]) -> str:
    version = (resource.get("meta") or {}).get("versionId")
    return f"{_reference(resource)}/_history/{version}" if version else _reference(resource)


def _frequency(timing: Any) -> Frequency | None:
    repeat = timing.get("repeat") if isinstance(timing, dict) else None
    if not isinstance(repeat, dict):
        code = _text(timing.get("code")) if isinstance(timing, dict) else ""
        return parse_frequency(code) if code else None
    try:
        count = float(repeat.get("frequency", 1))
        period = float(repeat["period"])
        hours = period * _PERIOD_HOURS[str(repeat.get("periodUnit", "d"))]
    except (KeyError, TypeError, ValueError):
        return None
    if count <= 0 or hours <= 0:
        return None
    per_day = count * 24.0 / hours
    interval = hours / count
    label = _PER_DAY_CODES.get(per_day) if hours >= 24 else None
    if label is None:
        label = f"Q{interval:g}H" if interval < 24 else f"Q{interval / 24:g}D"
    return Frequency(code=label, doses_per_day=per_day, interval_hours=interval)


def _route(concept: Any) -> Route | None:
    for coding in _codings(concept):
        if coding.system == SNOMED and coding.code in ROUTE_CODES:
            return ROUTE_CODES[coding.code]
    text = _text(concept)
    return parse_route(text) if text else None


def medication_sig(
    request: dict[str, Any], name: str, formulary: Formulary
) -> MedicationSig | None:
    dosages = request.get("dosageInstruction") or []
    dosage = dosages[0] if dosages and isinstance(dosages[0], dict) else None
    if dosage is None:
        return parse_sig(name, formulary) if any(ch.isdigit() for ch in name) else None
    text = str(dosage.get("text") or name)
    parsed = parse_sig(f"{name} {text}" if text != name else name, formulary)
    dose = parsed.dose
    for dose_and_rate in dosage.get("doseAndRate") or []:
        quantity = dose_and_rate.get("doseQuantity") if isinstance(dose_and_rate, dict) else None
        if isinstance(quantity, dict) and isinstance(quantity.get("value"), int | float):
            unit = str(quantity.get("code") or quantity.get("unit") or "")
            unit = {"[iU]": "unit", "U": "unit", "{puff}": "puff", "{tbl}": "tablet"}.get(
                unit, unit
            )
            if quantity["value"] > 0 and unit:
                dose = Dose(value=float(quantity["value"]), unit=unit)
                break
    route = _route(dosage.get("route")) or parsed.route
    frequency = _frequency(dosage.get("timing")) or parsed.frequency
    prn = bool(dosage.get("asNeededBoolean")) or "asNeededCodeableConcept" in dosage or parsed.prn
    return parsed.model_copy(
        update={"text": text, "dose": dose, "route": route, "frequency": frequency, "prn": prn}
    )


def _medication_concept(request: dict[str, Any]) -> Any:
    if "medicationCodeableConcept" in request:
        return request["medicationCodeableConcept"]
    reference = (request.get("medicationReference") or {}).get("reference", "")
    if isinstance(reference, str) and reference.startswith("#"):
        for contained in request.get("contained") or []:
            if isinstance(contained, dict) and contained.get("id") == reference[1:]:
                return contained.get("code")
    return None


def chart_from_resources(
    patient_id: str,
    conditions: list[dict[str, Any]],
    requests: list[dict[str, Any]],
    allergies: list[dict[str, Any]],
    formulary: Formulary,
) -> ChartSnapshot:
    excluded = {"entered-in-error", "refuted"}
    chart_conditions = [
        ChartCondition(
            reference=_reference(c),
            name=_text(c.get("code")) or "unnamed condition",
            coding=_codings(c.get("code")),
            clinical_status=_status(c.get("clinicalStatus"), "active"),
        )
        for c in conditions
        if _status(c.get("verificationStatus"), "confirmed") not in excluded
    ]
    chart_meds = []
    for request in requests:
        concept = _medication_concept(request)
        name = _text(concept) or "unnamed medication"
        rxnorm = next((c.code for c in _codings(concept) if c.system == RXNORM), None)
        drug = (formulary.get(rxnorm) if rxnorm else None) or formulary.resolve(name)
        chart_meds.append(
            ChartMedication(
                reference=_reference(request),
                name=name,
                rxnorm=drug.rxnorm if drug else rxnorm,
                sig=medication_sig(request, name, formulary),
                status=str(request.get("status", "unknown")),
            )
        )
    chart_allergies = []
    for allergy in allergies:
        if _status(allergy.get("verificationStatus"), "confirmed") in excluded:
            continue
        if _status(allergy.get("clinicalStatus"), "active") in {"inactive", "resolved"}:
            continue
        concept = allergy.get("code")
        chart_allergies.append(
            ChartAllergy(
                reference=_reference(allergy),
                substance=_text(concept) or "unspecified substance",
                rxnorm=next((c.code for c in _codings(concept) if c.system == RXNORM), None),
                criticality=allergy.get("criticality"),
            )
        )
    return ChartSnapshot(
        patient_reference=f"Patient/{patient_id}",
        conditions=tuple(chart_conditions),
        medications=tuple(chart_meds),
        allergies=tuple(chart_allergies),
        versions=tuple(_versioned(r) for r in (*conditions, *requests, *allergies)),
    )


class FhirChartSource:
    def __init__(self, client: FhirClient, formulary: Formulary) -> None:
        self._client = client
        self._formulary = formulary

    async def chart(self, patient_id: str) -> ChartSnapshot:
        try:
            await self._client.read("Patient", patient_id)
        except NotFoundError as exc:
            raise NotFoundError(f"patient {patient_id} not found on the FHIR server") from exc
        conditions, requests, allergies = await asyncio.gather(
            self._client.search("Condition", {"patient": patient_id, "_count": "100"}),
            self._client.search(
                "MedicationRequest",
                {"patient": patient_id, "status": "active,on-hold", "_count": "100"},
            ),
            self._client.search("AllergyIntolerance", {"patient": patient_id, "_count": "100"}),
        )
        return chart_from_resources(patient_id, conditions, requests, allergies, self._formulary)


class FhirRecordWriter:
    """Writes the approved DocumentReference and its Provenance in one atomic transaction."""

    def __init__(self, client: FhirClient, clock: Callable[[], datetime]) -> None:
        self._client = client
        self._clock = clock

    async def write(
        self, *, document_reference: dict[str, Any], provenance: dict[str, Any]
    ) -> FhirWriteResult:
        doc_url = f"urn:uuid:{uuid.uuid4()}"
        identifier = (document_reference.get("identifier") or [{}])[0]
        provenance = {**provenance, "target": [{"reference": doc_url}]}
        bundle = {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [
                {
                    "fullUrl": doc_url,
                    "resource": document_reference,
                    "request": {
                        "method": "POST",
                        "url": "DocumentReference",
                        "ifNoneExist": (
                            f"identifier={identifier.get('system')}|{identifier.get('value')}"
                        ),
                    },
                },
                {
                    "fullUrl": f"urn:uuid:{uuid.uuid4()}",
                    "resource": provenance,
                    "request": {"method": "POST", "url": "Provenance"},
                },
            ],
        }
        response = await self._client.transaction(bundle)
        entries = response.get("entry") or []
        if len(entries) != 2:
            raise UpstreamError("FHIR transaction response does not match the request")
        locations = []
        for entry in entries:
            location = ((entry or {}).get("response") or {}).get("location")
            if not location:
                raise UpstreamError("FHIR transaction response lacks a resource location")
            locations.append(_strip_history(str(location), self._client.base_url))
        return FhirWriteResult(
            document_reference=locations[0], provenance=locations[1], written_at=self._clock()
        )


def _strip_history(location: str, base: str) -> str:
    relative = location.removeprefix(base).lstrip("/")
    parts = relative.split("/")
    return "/".join(parts[:2]) if len(parts) >= 2 else relative


def now_utc() -> datetime:
    return datetime.now(UTC)
