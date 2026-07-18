from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from services.api.persistence_models import ReviewDecision
from services.api.persistence_repository import SQLitePersistenceRepository

REVIEW_DECISION_PERMISSIONS = {
    "approved": "review_events:approve",
    "edited": "review_events:edit",
    "rejected": "review_events:approve",
}


@dataclass(frozen=True)
class AuthoritativeReviewDecision:
    decision_id: str
    version: int
    review_item_id: str
    item_version: str
    artifact_id: str
    decision: str
    reason: str
    actor_id: str
    actor_role: str
    decided_at: str
    audit_event_id: str

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["actor"] = {
            "id": payload.pop("actor_id"),
            "role": payload.pop("actor_role"),
        }
        return payload


def record_authoritative_review_decision(
    repository: SQLitePersistenceRepository,
    *,
    decision_id: str,
    decision_version: int,
    review_item_id: str,
    item_version: str,
    artifact_id: str,
    actor_id: str,
    actor_role: str,
    decision: str,
    reason: str,
    high_risk: bool,
) -> AuthoritativeReviewDecision:
    from services.api.poc_web import ROLE_PERMISSIONS

    normalized_role = _required_text(actor_role, "actor_role")
    normalized_decision = _required_text(decision, "decision").lower()
    required_permission = REVIEW_DECISION_PERMISSIONS.get(normalized_decision)
    role_permissions = ROLE_PERMISSIONS.get(normalized_role)
    if (
        required_permission is None
        or role_permissions is None
        or required_permission not in role_permissions
    ):
        raise PermissionError(
            f"role {normalized_role!r} cannot record decision {normalized_decision!r}"
        )
    if not isinstance(decision_version, int) or isinstance(decision_version, bool):
        raise ValueError("decision_version must be a positive integer")
    if decision_version < 1:
        raise ValueError("decision_version must be a positive integer")

    normalized_review_item_id = _required_text(review_item_id, "review_item_id")
    normalized_item_version = _required_text(item_version, "item_version")
    normalized_reason = _required_text(reason, "reason")
    normalized_actor_id = _required_text(actor_id, "actor_id")
    audit_event_id = f"audit-{_required_text(decision_id, 'decision_id')}"

    with repository.transaction() as transaction:
        review_item = transaction.get_review_item(normalized_review_item_id)
        if review_item is None:
            raise ValueError(
                f"review item {normalized_review_item_id!r} does not exist"
            )
        effective_high_risk = (
            bool(high_risk) or review_item.severity.strip().lower() == "high"
        )
        if effective_high_risk and (
            normalized_role not in {"approver", "admin"}
            or normalized_decision != "approved"
        ):
            raise ValueError("high-risk review item requires approver approval")
        if review_item.status.strip().lower() == "open":
            transaction.update_review_item_status(
                normalized_review_item_id,
                status="closed",
            )
        stored = transaction.create_review_decision(
            decision_id=decision_id,
            review_item_id=normalized_review_item_id,
            artifact_id=artifact_id,
            actor=normalized_actor_id,
            role=normalized_role,
            decision=normalized_decision,
        )
        transaction.create_audit_event(
            event_id=audit_event_id,
            job_id=stored.job_id,
            document_id=stored.document_id,
            actor=normalized_actor_id,
            action=normalized_decision,
            scope_type="review_decision",
            scope_id=stored.decision_id,
            payload=_review_decision_audit_payload(
                stored,
                decision_version=decision_version,
                item_version=normalized_item_version,
                reason=normalized_reason,
            ),
        )

    with repository.transaction() as snapshot:
        persisted = snapshot.get_review_decision(decision_id)
        audit_event = snapshot.get_audit_event(audit_event_id)
    if persisted is None or audit_event is None:
        raise RuntimeError("authoritative review decision persistence was incomplete")
    audit_payload = json.loads(audit_event.payload_json)
    if (
        audit_payload.get("decision_version") != decision_version
        or audit_payload.get("item_version") != normalized_item_version
        or audit_payload.get("reason") != normalized_reason
    ):
        raise RuntimeError("authoritative review decision audit snapshot did not match")
    return AuthoritativeReviewDecision(
        decision_id=persisted.decision_id,
        version=decision_version,
        review_item_id=persisted.review_item_id,
        item_version=normalized_item_version,
        artifact_id=persisted.artifact_id,
        decision=persisted.decision,
        reason=normalized_reason,
        actor_id=persisted.actor,
        actor_role=persisted.role,
        decided_at=persisted.created_at,
        audit_event_id=audit_event.event_id,
    )


def _review_decision_audit_payload(
    decision: ReviewDecision,
    *,
    decision_version: int,
    item_version: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "actor": {"id": decision.actor, "role": decision.role},
        "artifact_id": decision.artifact_id,
        "decided_at": decision.created_at,
        "decision": decision.decision,
        "decision_version": decision_version,
        "item_version": item_version,
        "reason": reason,
        "review_item_id": decision.review_item_id,
    }


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()
