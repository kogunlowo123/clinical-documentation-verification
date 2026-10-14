"""Load profile for a deployed service.

Run against a staging deployment backed by a synthetic-data FHIR server, never production:

    CDV_LOAD_TOKEN=<agent or clinician credential> CDV_LOAD_PATIENT=<synthetic patient id> \
    locust -f tests/performance/locustfile.py --host https://cdv.staging.internal \
           --users 50 --spawn-rate 5 --run-time 10m --headless

Ambient scribes submit far less often than clinicians read and check orders, so
the mix weights reads and order validation over full verifications.
"""

from __future__ import annotations

import os

from locust import HttpUser, between, task

TOKEN = os.environ.get("CDV_LOAD_TOKEN", "")
PATIENT = os.environ.get("CDV_LOAD_PATIENT", "")
TRANSCRIPT = (
    "Doctor: How have you been since the last visit?\n"
    "Patient: I take lisinopril 10 mg daily and atorvastatin 40 mg at bedtime.\n"
    "Doctor: Continue lisinopril 10 mg by mouth daily. Let's check a lipid panel.\n"
)


class DocumentationTraffic(HttpUser):
    wait_time = between(0.5, 2.0)

    def on_start(self) -> None:
        self.client.headers.update({"Authorization": f"Bearer {TOKEN}"})

    @task(1)
    def verify(self) -> None:
        self.client.post(
            "/v1/verifications",
            json={
                "patient_id": PATIENT,
                "encounter_id": "load-test",
                "transcript": {"text": TRANSCRIPT},
            },
            name="/v1/verifications [submit]",
        )

    @task(5)
    def list_pending(self) -> None:
        self.client.get(
            "/v1/verifications",
            params={"patient_id": PATIENT, "status": "pending_approval"},
            name="/v1/verifications [list]",
        )

    @task(3)
    def validate_orders(self) -> None:
        self.client.post(
            "/v1/orders/validate",
            json={
                "orders": [
                    {"text": "amoxicillin 500 mg PO TID"},
                    {"text": "ibuprofen 400 mg PO q6h"},
                ]
            },
            name="/v1/orders/validate",
        )
