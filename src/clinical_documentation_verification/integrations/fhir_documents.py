"""Build the only resources this service ever writes: DocumentReference and Provenance.

The DocumentReference carries the clinician-approved note (``docStatus =
final``, authenticator = the signing practitioner). The Provenance records
the clinician as author, verifier and signer, this agent as assembler, and
every chart resource version and the transcript the note was derived from.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime
from typing import Any

from clinical_documentation_verification.domain.models import Verification

LOINC = "http://loinc.org"
URI = "urn:ietf:rfc:3986"
NOTE_TYPES: dict[str, str] = {
    "11506-3": "Progress note",
    "34117-2": "History and physical note",
    "11488-4": "Consult note",
    "18842-5": "Discharge summary",
    "34111-5": "Emergency department note",
}
_PARTICIPANT = "http://terminology.hl7.org/CodeSystem/provenance-participant-type"


def attachment_hash(data: bytes) -> str:
    """FHIR R4 ``Attachment.hash``: base64 SHA-1 of the data, fixed by the specification.

    It is an integrity check for consumers, not a security control; the
    signed audit chain and the Provenance signature carry the trust.
    """
    digest = hashlib.sha1(data, usedforsecurity=False).digest()  # nosemgrep
    return base64.b64encode(digest).decode()


def _participant(code: str) -> dict[str, Any]:
    return {"coding": [{"system": _PARTICIPANT, "code": code}]}


def document_reference(
    verification: Verification, *, note_text: str, practitioner: str, at: datetime
) -> dict[str, Any]:
    data = note_text.encode("utf-8")
    title = NOTE_TYPES.get(verification.note_type, "Clinical note")
    return {
        "resourceType": "DocumentReference",
        "identifier": [{"system": URI, "value": f"urn:uuid:{verification.id}"}],
        "status": "current",
        "docStatus": "final",
        "type": {"coding": [{"system": LOINC, "code": verification.note_type, "display": title}]},
        "category": [
            {
                "coding": [
                    {
                        "system": "http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category",
                        "code": "clinical-note",
                        "display": "Clinical Note",
                    }
                ]
            }
        ],
        "subject": {"reference": f"Patient/{verification.patient_id}"},
        "date": at.isoformat(),
        "author": [{"reference": practitioner}],
        "authenticator": {"reference": practitioner},
        "description": "Clinician-verified encounter documentation",
        "securityLabel": [
            {
                "system": "http://terminology.hl7.org/CodeSystem/v3-Confidentiality",
                "code": "R",
                "display": "restricted",
            }
        ],
        "content": [
            {
                "attachment": {
                    "contentType": "text/plain; charset=utf-8",
                    "language": "en-US",
                    "data": base64.b64encode(data).decode(),
                    "hash": attachment_hash(data),
                    "size": len(data),
                    "title": title,
                    "creation": at.isoformat(),
                }
            }
        ],
        "context": {"encounter": [{"reference": f"Encounter/{verification.encounter_id}"}]},
    }


def provenance(
    verification: Verification, *, practitioner: str, agent_id: str, at: datetime
) -> dict[str, Any]:
    sources: list[dict[str, Any]] = [
        {
            "role": "derivation",
            "what": {
                "identifier": {"system": URI, "value": f"urn:uuid:{verification.id}"},
                "display": f"Encounter transcript {verification.transcript.digest}",
            },
        }
    ]
    sources.extend(
        {"role": "source", "what": {"reference": version}}
        for version in verification.chart.versions
    )
    return {
        "resourceType": "Provenance",
        "recorded": at.isoformat(),
        "activity": {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/v3-DataOperation",
                    "code": "CREATE",
                    "display": "create",
                }
            ]
        },
        "reason": [
            {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/v3-ActReason",
                        "code": "TREAT",
                        "display": "treatment",
                    }
                ]
            }
        ],
        "agent": [
            {"type": _participant("author"), "who": {"reference": practitioner}},
            {"type": _participant("verifier"), "who": {"reference": practitioner}},
            {
                "type": _participant("assembler"),
                "who": {
                    "identifier": {"system": URI, "value": agent_id},
                    "display": "clinical documentation verification agent",
                },
            },
        ],
        "entity": sources,
        "signature": [
            {
                "type": [
                    {
                        "system": "urn:iso-astm:E1762-95:2013",
                        "code": "1.2.840.10065.1.12.1.1",
                        "display": "Author's Signature",
                    }
                ],
                "when": at.isoformat(),
                "who": {"reference": practitioner},
            }
        ],
    }
