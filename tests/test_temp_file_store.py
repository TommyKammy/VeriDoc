from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from services.api.temp_file_store import TemporaryFileStore, _metadata_mac


TEST_KEY = b"0123456789abcdef0123456789abcdef"


def test_temp_file_store_encrypts_tracks_retention_and_deletes_missing_safely(tmp_path):
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    store = TemporaryFileStore(root=tmp_path, encryption_key=TEST_KEY, now=lambda: now)

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


def test_temp_file_store_resolves_relative_root_at_construction(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    current = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    store = TemporaryFileStore(
        root=Path("relative-store"),
        encryption_key=TEST_KEY,
        now=lambda: current,
    )

    record = store.save(
        category="upload",
        filename="payload.txt",
        content=b"payload",
        retention=timedelta(hours=1),
    )

    assert record.storage_root == (tmp_path / "relative-store").resolve()
    assert record.path == record.storage_root / "upload" / f"{record.artifact_id}.bin"
    assert store.read(record.artifact_id) == b"payload"


def test_temp_file_store_cleanup_expired_removes_only_expired_artifacts(tmp_path):
    current = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    store = TemporaryFileStore(root=tmp_path, encryption_key=TEST_KEY, now=lambda: current)

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


def test_temp_file_store_read_rejects_expired_artifact_before_returning_bytes(tmp_path):
    current = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    store = TemporaryFileStore(root=tmp_path, encryption_key=TEST_KEY, now=lambda: current)
    expired = store.save(
        category="upload",
        filename="expired.txt",
        content=b"expired",
        retention=timedelta(seconds=1),
        created_at=current - timedelta(minutes=5),
    )

    with pytest.raises(FileNotFoundError, match="expired"):
        store.read(expired.artifact_id)

    assert not expired.path.exists()
    assert not expired.metadata_path.exists()


def test_temp_file_store_read_authenticates_expiry_metadata_before_trusting_it(tmp_path):
    current = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    store = TemporaryFileStore(root=tmp_path, encryption_key=TEST_KEY, now=lambda: current)
    expired = store.save(
        category="upload",
        filename="expired.txt",
        content=b"expired",
        retention=timedelta(seconds=1),
        created_at=current - timedelta(minutes=5),
    )
    metadata = json.loads(expired.metadata_path.read_text(encoding="utf-8"))
    metadata["expires_at"] = (current + timedelta(hours=1)).isoformat()
    expired.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(RuntimeError, match="metadata integrity"):
        store.read(expired.artifact_id)

    assert expired.path.exists()
    assert expired.metadata_path.exists()


def test_temp_file_store_read_rejects_authenticated_variable_length_nonce(tmp_path):
    current = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    store = TemporaryFileStore(root=tmp_path, encryption_key=TEST_KEY, now=lambda: current)
    record = store.save(
        category="upload",
        filename="payload.txt",
        content=b"payload",
        retention=timedelta(hours=1),
    )
    metadata = json.loads(record.metadata_path.read_text(encoding="utf-8"))
    metadata["nonce_hex"] = metadata["nonce_hex"] + "00"
    metadata["metadata_hmac_sha256"] = _metadata_mac(metadata, key=TEST_KEY)
    record.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(ValueError, match="nonce length"):
        store.read(record.artifact_id)


def test_temp_file_store_rejects_placeholder_encryption_key(tmp_path):
    with pytest.raises(ValueError, match="placeholder"):
        TemporaryFileStore(root=tmp_path, encryption_key=b"TODO")


def test_temp_file_store_rejects_tampered_metadata_path_before_unlinking(tmp_path):
    current = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    store = TemporaryFileStore(root=tmp_path / "store", encryption_key=TEST_KEY, now=lambda: current)
    record = store.save(
        category="upload",
        filename="payload.txt",
        content=b"payload",
        retention=timedelta(hours=1),
    )
    outside_path = tmp_path / "outside.bin"
    outside_path.write_bytes(b"outside")
    metadata = json.loads(record.metadata_path.read_text(encoding="utf-8"))
    metadata["path"] = str(record.storage_root / ".." / outside_path.name)
    record.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(RuntimeError, match="metadata integrity"):
        store.delete(record.artifact_id)
    assert outside_path.read_bytes() == b"outside"
    assert record.metadata_path.exists()


def test_temp_file_store_cleanup_validates_metadata_id_against_filename(tmp_path):
    current = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    store = TemporaryFileStore(root=tmp_path, encryption_key=TEST_KEY, now=lambda: current)
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
    metadata = json.loads(expired.metadata_path.read_text(encoding="utf-8"))
    metadata["artifact_id"] = retained.artifact_id
    expired.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    assert store.cleanup_expired() == []
    assert expired.path.exists()
    assert expired.metadata_path.exists()
    assert retained.path.exists()
    assert retained.metadata_path.exists()


def test_temp_file_store_save_rejects_symlinked_category_directory(tmp_path):
    store_root = tmp_path / "store"
    outside_root = tmp_path / "outside"
    store_root.mkdir()
    outside_root.mkdir()
    (store_root / "upload").symlink_to(outside_root, target_is_directory=True)
    store = TemporaryFileStore(root=store_root, encryption_key=TEST_KEY)

    with pytest.raises(ValueError, match="category directory"):
        store.save(
            category="upload",
            filename="payload.txt",
            content=b"payload",
            retention=timedelta(hours=1),
        )

    assert list(outside_root.iterdir()) == []


def test_temp_file_store_missing_metadata_fallback_delete_skips_symlinked_category(
    tmp_path,
):
    artifact_id = "tmp-" + "1" * 32
    store_root = tmp_path / "store"
    outside_root = tmp_path / "outside"
    real_category_root = store_root / "real"
    symlinked_category_root = store_root / "upload"
    store_root.mkdir()
    outside_root.mkdir()
    real_category_root.mkdir()
    symlinked_category_root.symlink_to(outside_root, target_is_directory=True)
    outside_artifact = outside_root / f"{artifact_id}.bin"
    real_artifact = real_category_root / f"{artifact_id}.bin"
    outside_artifact.write_bytes(b"outside")
    real_artifact.write_bytes(b"real")
    store = TemporaryFileStore(root=store_root, encryption_key=TEST_KEY)

    assert store.delete(artifact_id) is True

    assert outside_artifact.read_bytes() == b"outside"
    assert not real_artifact.exists()
