"""Export published contracts: JSON Schemas for inputs/outputs and the OpenAPI document.

python -m clinical_documentation_verification.contracts          # rewrite schemas/, docs/api/
python -m clinical_documentation_verification.contracts --check  # exit 1 when stale
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from clinical_documentation_verification.api.app import create_app
from clinical_documentation_verification.api.schemas import VerificationSummary, VerificationView
from clinical_documentation_verification.application.commands import (
    ApproveVerification,
    OrderValidationResult,
    RejectVerification,
    SubmitVerification,
    ValidateOrders,
)
from clinical_documentation_verification.domain.models import MedicationSig
from clinical_documentation_verification.orchestration.extraction_agent import ModelExtraction

MODELS: dict[str, type[BaseModel]] = {
    "submit-verification.request": SubmitVerification,
    "verification.response": VerificationView,
    "verification-summary.response": VerificationSummary,
    "approve-verification.request": ApproveVerification,
    "reject-verification.request": RejectVerification,
    "validate-orders.request": ValidateOrders,
    "order-validation.response": OrderValidationResult,
    "medication-sig.response": MedicationSig,
    "model-extraction.output": ModelExtraction,
}


async def _no_container() -> Any:  # pragma: no cover - never started
    raise RuntimeError("contract export does not start the application")


def documents() -> dict[str, dict[str, Any]]:
    docs: dict[str, dict[str, Any]] = {
        f"schemas/{name}.schema.json": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://github.com/kogunlowo123/clinical-documentation-verification/"
            f"schemas/{name}.schema.json",
            **model.model_json_schema(mode="validation"),
        }
        for name, model in MODELS.items()
    }
    app: FastAPI = create_app(_no_container)
    docs["docs/api/openapi.json"] = app.openapi()
    return docs


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    base = root or Path.cwd()
    stale: list[str] = []
    for relative, document in documents().items():
        path = base / relative
        content = render(document)
        if "--check" in args:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(relative)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
    if stale:
        sys.stderr.write(
            "stale contracts (run python -m clinical_documentation_verification.contracts): "
            + ", ".join(stale)
            + "\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
