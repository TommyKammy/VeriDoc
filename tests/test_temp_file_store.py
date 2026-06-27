from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.api.temp_file_store import TemporaryFileStore


def test_temp_file_store_encrypts_tracks_retention_and_deletes_missing_safely(tmp_path):
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    store = TemporaryFileStore(root=tmp_path, encryption_key=b"test-key", now=lambda: now)

    record = store.save(
        category="upload",
        filename="batch-record.pdf",
        content=b"confidential batch payload",
        retention=timedelta(hours=6),
    )

    assert record.storage_root == tmp_path
    assert record.category == "upload"
    assert record.original_filename == "batch-record.pdf"
    assert record.expires_at == "2026-01-02T09:04:05+00:00"
    assert record.encryption == {
        "encrypted": True,
        "algorithm": "hmac-sha256-stream",
        "key_source": "configured",
    }
    assert record.path.is_file()
    assert record.metadata_path.is_file()
    assert b"confidential batch payload" not in record.path.read_bytes()
    assert store.read(record.artifact_id) == b"confidential batch payload"

    assert store.delete(record.artifact_id) is True
    assert store.delete(record.artifact_id) is False
    assert not record.path.exists()
    assert not record.metadata_path.exists()


def test_temp_file_store_cleanup_expired_removes_only_expired_artifacts(tmp_path):
    current = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    store = TemporaryFileStore(root=tmp_path, encryption_key=b"test-key", now=lambda: current)

    expired = store.save(
        category="upload",
        filename="expired.txt",
        content=b"expired",
        retention=timedelta(seconds=1),
        created_at=current - timedelta(minutes=5),
    )
    retained = store.save(
        category="result",
        filename="retained.txt",
        content=b"retained",
        retention=timedelta(hours=1),
    )

    assert store.cleanup_expired() == [expired.artifact_id]
    assert not expired.path.exists()
    assert not expired.metadata_path.exists()
    assert retained.path.exists()
    assert store.read(retained.artifact_id) == b"retained"
