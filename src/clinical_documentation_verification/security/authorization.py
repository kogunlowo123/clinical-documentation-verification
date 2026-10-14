"""Authorization: local domain policy plus, for fabric-authenticated agents, the fabric's decision.

The domain policy (``policies/documentation-verification.yaml``) holds the
clinical rules: only clinicians approve, critical findings need an override
reason, chart writes need a clinician signature. For agents authenticated
through the identity fabric, the fabric's ``/v1/authorize`` decision (scopes,
delegation chain, classification ceiling) is combined with the local one
using deny-overrides, then require-approval, then allow. Every decision is
written to the signed audit chain.
"""

from __future__ import annotations

import fnmatch
from typing import Any

from agentic_core.audit import AuditLog
from agentic_core.domain.classification import DataClassification, Decision, RiskLevel
from agentic_core.observability import correlation_id
from agentic_core.policy import PolicyEngine, PolicyRequest, Resource, Subject

from clinical_documentation_verification.domain.errors import ForbiddenError
from clinical_documentation_verification.domain.models import PolicyOutcome
from clinical_documentation_verification.integrations.identity import IdentityFabricClient
from clinical_documentation_verification.security.principals import Principal

RISK: dict[str, RiskLevel] = {
    "documentation:read": RiskLevel.LOW,
    "orders:validate": RiskLevel.LOW,
    "documentation:verify": RiskLevel.MODERATE,
    "documentation:reject": RiskLevel.MODERATE,
    "documentation:approve": RiskLevel.HIGH,
    "fhir:write:DocumentReference": RiskLevel.HIGH,
}
_ORDER = {Decision.DENY: 0, Decision.REQUIRE_APPROVAL: 1, Decision.ALLOW: 2}


def _combine(local: Decision, remote: Decision | None) -> Decision:
    if remote is None:
        return local
    return min(local, remote, key=lambda d: _ORDER[d])


class AccessController:
    def __init__(
        self,
        policy: PolicyEngine,
        audit: AuditLog,
        *,
        fabric: IdentityFabricClient | None = None,
    ) -> None:
        self._policy = policy
        self._audit = audit
        self._fabric = fabric

    @property
    def policy(self) -> PolicyEngine:
        return self._policy

    @staticmethod
    def subject(principal: Principal, action: str) -> Subject:
        return Subject(
            id=principal.subject,
            kind=principal.kind,
            roles=principal.roles,
            attributes={
                "scopes": sorted(principal.scopes),
                "action_in_scope": any(fnmatch.fnmatchcase(action, s) for s in principal.scopes),
                "delegated": principal.delegated,
                **({"fhir_user": principal.fhir_user} if principal.fhir_user else {}),
                **(
                    {"fhir_user_type": principal.fhir_user_type} if principal.fhir_user_type else {}
                ),
            },
        )

    async def decide(
        self,
        subject: Subject,
        action: str,
        resource: Resource,
        environment: dict[str, Any],
        *,
        fabric_token: str | None = None,
    ) -> PolicyOutcome:
        local = self._policy.evaluate(
            PolicyRequest(
                subject=subject,
                action=action,
                resource=resource,
                risk=RISK.get(action, RiskLevel.MODERATE),
                environment=environment,
            )
        )
        remote = None
        if fabric_token is not None and self._fabric is not None:
            remote = await self._fabric.authorize(
                fabric_token,
                action=action,
                resource_type=resource.type,
                resource_id=resource.id,
                classification=resource.classification,
                attributes={k: v for k, v in resource.attributes.items() if _scalar(v)},
                environment={"purpose_of_use": "TREAT", **environment},
            )
        decision = _combine(local.decision, remote.decision if remote else None)
        reason = local.reason
        if remote is not None and remote.decision is decision and decision is not local.decision:
            reason = f"identity fabric: {remote.reason}"
        outcome = PolicyOutcome(
            action=action,
            decision=decision.value,
            matched_rules=local.matched_rules,
            policy_version=local.policy_version,
            policy_digest=local.policy_digest,
            reason=reason,
            fabric_decision=remote.decision.value if remote else None,
        )
        await self._audit.record(
            actor=subject.id,
            action=f"authorize:{action}",
            resource=f"{resource.type}:{resource.id}",
            outcome=outcome.decision,
            correlation_id=correlation_id(),
            details={
                "rules": list(outcome.matched_rules),
                "policy_digest": outcome.policy_digest,
                "fabric_decision": outcome.fabric_decision,
            },
        )
        return outcome

    async def require(
        self,
        principal: Principal,
        action: str,
        *,
        resource_type: str,
        resource_id: str = "*",
        attributes: dict[str, Any] | None = None,
        environment: dict[str, Any] | None = None,
    ) -> PolicyOutcome:
        """Decide for an inbound caller; raise ``ForbiddenError`` on deny."""
        outcome = await self.decide(
            self.subject(principal, action),
            action,
            Resource(
                type=resource_type,
                id=resource_id,
                classification=DataClassification.RESTRICTED,
                attributes=attributes or {},
            ),
            environment or {},
            fabric_token=principal.token if principal.via == "fabric" else None,
        )
        if outcome.decision == Decision.DENY.value:
            rules = ", ".join(outcome.matched_rules) or "default deny"
            raise ForbiddenError(f"{action} denied ({rules}): {outcome.reason}")
        return outcome


def _scalar(value: Any) -> bool:
    return isinstance(value, str | int | float | bool) or value is None
