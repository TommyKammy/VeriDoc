from dataclasses import fields

from services.api import (
    persistence,
    persistence_contracts,
    persistence_models,
    persistence_repository,
)


def test_sqlite_repository_is_compatibly_reexported_from_dedicated_module() -> None:
    assert (
        persistence.SQLitePersistenceRepository
        is persistence_repository.SQLitePersistenceRepository
    )
    assert (
        persistence_repository.SQLitePersistenceRepository.__module__
        == "services.api.persistence_repository"
    )


def test_persistence_models_are_compatibly_reexported_from_dedicated_module() -> None:
    expected_fields = {
        "Document": (
            "document_id", "source_type", "original_filename", "source_artifact_id",
            "source_storage_key", "content_hash", "status", "uploaded_by",
            "created_at", "updated_at",
        ),
        "SourceArtifact": (
            "artifact_id", "document_id", "storage_key", "content_hash",
            "source_type", "original_filename", "uploaded_by", "created_at",
        ),
        "ConversionJob": (
            "job_id", "document_id", "idempotency_key", "mode", "status",
            "attempts", "created_at", "updated_at",
        ),
        "JobEvent": (
            "event_id", "job_id", "sequence", "event_type", "actor",
            "payload_json", "created_at",
        ),
        "ConversionResult": (
            "result_id", "job_id", "document_id", "status", "content_hash",
            "created_at", "updated_at",
        ),
        "Artifact": (
            "artifact_id", "result_id", "job_id", "document_id", "category",
            "format", "display_filename", "storage_key", "content_hash",
            "retention_state", "created_at", "updated_at",
        ),
        "ReviewItem": (
            "review_item_id", "document_id", "job_id", "target_path", "status",
            "severity", "created_at", "updated_at",
        ),
        "ReviewDecision": (
            "decision_id", "review_item_id", "artifact_id", "job_id",
            "document_id", "actor", "role", "decision", "created_at", "updated_at",
        ),
        "AuditEvent": (
            "event_id", "job_id", "document_id", "sequence", "integrity_algorithm",
            "actor", "action", "scope_type", "scope_id", "event_hash",
            "prev_event_hash", "payload_json", "created_at",
        ),
    }

    for name, field_names in expected_fields.items():
        model = getattr(persistence_models, name)
        assert getattr(persistence, name) is model
        assert model.__module__ == "services.api.persistence_models"
        assert tuple(field.name for field in fields(model)) == field_names

    assert persistence._AuditActionContract is persistence_contracts._AuditActionContract
    assert persistence._AUDIT_ACTION_CONTRACTS is persistence_contracts._AUDIT_ACTION_CONTRACTS
    assert (
        persistence._AUDIT_SCOPE_PAYLOAD_BINDINGS
        is persistence_contracts._AUDIT_SCOPE_PAYLOAD_BINDINGS
    )
