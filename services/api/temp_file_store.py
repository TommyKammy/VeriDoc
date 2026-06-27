from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
from typing import Any, Callable


ENCRYPTION_ALGORITHM = "hmac-sha256-stream"
ARTIFACT_ID_PATTERN = re.compile(r"tmp-[0-9a-f]{32}")
MIN_ENCRYPTION_KEY_BYTES = 32
NONCE_BYTES = 16
METADATA_AUTH_FIELDS = (
    "artifact_id",
    "category",
    "original_filename",
    "storage_root",
    "path",
    "metadata_path",
    "created_at",
    "expires_at",
    "sha256",
    "size_bytes",
    "encryption",
    "nonce_hex",
    "ciphertext_hmac_sha256",
)
PLACEHOLDER_ENCRYPTION_KEYS = {
    "changeme",
    "change_me",
    "change-me",
    "placeholder",
    "replace_me",
    "replace-me",
    "todo",
}


@dataclass(frozen=True)
class TemporaryFileRecord:
    artifact_id: str
    category: str
    original_filename: str
    storage_root: Path
    path: Path
    metadata_path: Path
    created_at: str
    expires_at: str
    sha256: str
    size_bytes: int
    encryption: dict[str, str | bool]

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "category": self.category,
            "original_filename": self.original_filename,
            "storage_root": str(self.storage_root),
            "path": str(self.path),
            "metadata_path": str(self.metadata_path),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "encryption": dict(self.encryption),
        }


class TemporaryFileStore:
    """Encrypted MVP temp artifact store for API uploads and conversion results."""

    def __init__(
        self,
        *,
        root: Path,
        encryption_key: bytes,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._key = _validate_encryption_key(encryption_key)
        self._root = root.resolve(strict=False)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def save(
        self,
        *,
        category: str,
        filename: str,
        content: bytes,
        retention: timedelta,
        created_at: datetime | None = None,
    ) -> TemporaryFileRecord:
        category = _validate_category(category)
        original_filename = _validate_filename(filename)
        if retention <= timedelta(0):
            raise ValueError("retention must be positive")
        if not isinstance(content, bytes):
            raise ValueError("content must be bytes")

        created = _as_utc(created_at or self._now())
        expires = created + retention
        artifact_id = f"tmp-{secrets.token_hex(16)}"
        artifact_dir = self._ensure_category_root(category)
        path = artifact_dir / f"{artifact_id}.bin"
        metadata_path = artifact_dir / f"{artifact_id}.json"

        nonce = secrets.token_bytes(NONCE_BYTES)
        encrypted = _crypt(content, key=self._key, nonce=nonce)
        mac = _mac(encrypted, key=self._key, nonce=nonce)
        path.write_bytes(encrypted)

        record = TemporaryFileRecord(
            artifact_id=artifact_id,
            category=category,
            original_filename=original_filename,
            storage_root=self._root,
            path=path,
            metadata_path=metadata_path,
            created_at=created.isoformat(),
            expires_at=expires.isoformat(),
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            encryption=_encryption_metadata(),
        )
        metadata = {
            **record.to_dict(),
            "nonce_hex": nonce.hex(),
            "ciphertext_hmac_sha256": mac,
        }
        metadata["metadata_hmac_sha256"] = _metadata_mac(metadata, key=self._key)
        metadata_path.write_text(
            json.dumps(metadata, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        return record

    def read(self, artifact_id: str) -> bytes:
        metadata = self._read_metadata(artifact_id)
        expires_at = _parse_datetime(_required_text(metadata, "expires_at"))
        if expires_at <= _as_utc(self._now()):
            self.delete(artifact_id)
            raise FileNotFoundError(f"temporary artifact expired: {artifact_id}")
        path = _artifact_path_from_metadata(self._root, metadata, artifact_id)
        nonce = bytes.fromhex(_required_text(metadata, "nonce_hex"))
        if len(nonce) != NONCE_BYTES:
            raise ValueError("temporary artifact nonce length is invalid")
        encrypted = path.read_bytes()
        expected_mac = _required_text(metadata, "ciphertext_hmac_sha256")
        if not hmac.compare_digest(_mac(encrypted, key=self._key, nonce=nonce), expected_mac):
            raise RuntimeError("temporary artifact integrity check failed")
        content = _crypt(encrypted, key=self._key, nonce=nonce)
        expected_sha256 = _required_text(metadata, "sha256")
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise RuntimeError("temporary artifact plaintext hash mismatch")
        return content

    def delete(self, artifact_id: str) -> bool:
        artifact_id = _validate_artifact_id(artifact_id)
        metadata_path = self._metadata_path_for_artifact(artifact_id)
        paths = [metadata_path]
        if metadata_path.is_file():
            metadata = _load_artifact_metadata(metadata_path, artifact_id, key=self._key)
            paths.insert(0, _artifact_path_from_metadata(self._root, metadata, artifact_id))
        else:
            paths.extend(_fallback_artifact_paths(self._root, artifact_id))

        removed = False
        for path in paths:
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                continue
        return removed

    def cleanup_expired(self) -> list[str]:
        now = _as_utc(self._now())
        removed: list[str] = []
        for metadata_path in _store_metadata_paths(self._root):
            try:
                artifact_id = _artifact_id_from_metadata_path(metadata_path)
                metadata = _load_artifact_metadata(metadata_path, artifact_id, key=self._key)
                expires_at = _parse_datetime(_required_text(metadata, "expires_at"))
            except (RuntimeError, ValueError, OSError):
                continue
            if expires_at <= now:
                try:
                    was_removed = self.delete(artifact_id)
                except (ValueError, OSError):
                    continue
                if was_removed:
                    removed.append(artifact_id)
        return removed

    def _read_metadata(self, artifact_id: str) -> dict[str, Any]:
        metadata_path = self._metadata_path_for_artifact(artifact_id)
        if not metadata_path.is_file():
            raise FileNotFoundError(f"temporary artifact not found: {artifact_id}")
        return _load_artifact_metadata(metadata_path, artifact_id, key=self._key)

    def _metadata_path_for_artifact(self, artifact_id: str) -> Path:
        artifact_id = _validate_artifact_id(artifact_id)
        matches = _store_metadata_paths(self._root, artifact_id=artifact_id)
        if len(matches) > 1:
            raise RuntimeError("temporary artifact id is ambiguous")
        if matches:
            return matches[0]
        return self._root / "_missing" / f"{artifact_id}.json"

    def _category_root(self, category: str) -> Path:
        return self._root / category

    def _ensure_category_root(self, category: str) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        category_root = self._category_root(category)
        if category_root.is_symlink():
            raise ValueError("temporary artifact category directory is invalid")
        category_root.mkdir(exist_ok=True)
        if category_root.is_symlink() or not category_root.is_dir():
            raise ValueError("temporary artifact category directory is invalid")
        return category_root


def _crypt(content: bytes, *, key: bytes, nonce: bytes) -> bytes:
    output = bytearray()
    counter = 0
    for offset in range(0, len(content), hashlib.sha256().digest_size):
        counter_bytes = counter.to_bytes(8, "big")
        block_key = hmac.new(key, nonce + counter_bytes, hashlib.sha256).digest()
        block = content[offset : offset + len(block_key)]
        output.extend(byte ^ block_key[index] for index, byte in enumerate(block))
        counter += 1
    return bytes(output)


def _mac(content: bytes, *, key: bytes, nonce: bytes) -> str:
    return hmac.new(key, nonce + content, hashlib.sha256).hexdigest()


def _encryption_metadata() -> dict[str, str | bool]:
    return {
        "encrypted": True,
        "algorithm": ENCRYPTION_ALGORITHM,
        "key_source": "configured",
    }


def _validate_encryption_key(encryption_key: bytes) -> bytes:
    key = bytes(encryption_key)
    if not key:
        raise ValueError("encryption_key is required")
    normalized = key.decode("utf-8", errors="ignore").strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    if normalized in PLACEHOLDER_ENCRYPTION_KEYS:
        raise ValueError("encryption_key must not be a placeholder value")
    if len(key) < MIN_ENCRYPTION_KEY_BYTES:
        raise ValueError("encryption_key must be at least 32 bytes")
    return key


def _validate_category(category: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", category):
        raise ValueError("category must use lowercase letters, numbers, hyphens, or underscores")
    return category


def _validate_filename(filename: str) -> str:
    name = Path(filename).name.strip()
    if not name or name in {".", ".."}:
        raise ValueError("filename is required")
    return name


def _validate_artifact_id(artifact_id: str) -> str:
    if not ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
        raise ValueError("artifact_id is invalid")
    return artifact_id


def _load_metadata(path: Path) -> dict[str, Any]:
    metadata = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("temporary artifact metadata must be an object")
    return metadata


def _load_artifact_metadata(
    metadata_path: Path,
    artifact_id: str,
    *,
    key: bytes | None = None,
) -> dict[str, Any]:
    artifact_id = _validate_artifact_id(artifact_id)
    if _artifact_id_from_metadata_path(metadata_path) != artifact_id:
        raise ValueError("temporary artifact metadata filename mismatch")
    metadata = _load_metadata(metadata_path)
    if _required_text(metadata, "artifact_id") != artifact_id:
        raise ValueError("temporary artifact metadata id mismatch")
    if key is not None:
        _verify_metadata_mac(metadata, key=key)
    return metadata


def _artifact_id_from_metadata_path(metadata_path: Path) -> str:
    if metadata_path.suffix != ".json":
        raise ValueError("temporary artifact metadata filename is invalid")
    return _validate_artifact_id(metadata_path.stem)


def _required_text(metadata: dict[str, Any], field_name: str) -> str:
    value = metadata.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"temporary artifact metadata missing {field_name}")
    return value


def _metadata_mac(metadata: dict[str, Any], *, key: bytes) -> str:
    try:
        payload = {field_name: metadata[field_name] for field_name in METADATA_AUTH_FIELDS}
    except KeyError as exc:
        raise ValueError("temporary artifact metadata missing authenticated field") from exc
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hmac.new(key, encoded, hashlib.sha256).hexdigest()


def _verify_metadata_mac(metadata: dict[str, Any], *, key: bytes) -> None:
    expected_mac = _required_text(metadata, "metadata_hmac_sha256")
    actual_mac = _metadata_mac(metadata, key=key)
    if not hmac.compare_digest(actual_mac, expected_mac):
        raise RuntimeError("temporary artifact metadata integrity check failed")


def _path_from_metadata(root: Path, metadata: dict[str, Any], field_name: str) -> Path:
    root = root.resolve(strict=False)
    path = Path(_required_text(metadata, field_name)).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("temporary artifact path escapes storage root") from exc
    return path


def _artifact_path_from_metadata(root: Path, metadata: dict[str, Any], artifact_id: str) -> Path:
    path = _path_from_metadata(root, metadata, "path")
    if path.name != f"{_validate_artifact_id(artifact_id)}.bin":
        raise ValueError("temporary artifact path does not match metadata id")
    return path


def _store_category_roots(root: Path) -> list[Path]:
    try:
        children = sorted(root.iterdir())
    except FileNotFoundError:
        return []
    return [child for child in children if not child.is_symlink() and child.is_dir()]


def _store_metadata_paths(root: Path, *, artifact_id: str | None = None) -> list[Path]:
    if artifact_id is not None:
        artifact_id = _validate_artifact_id(artifact_id)
    paths: list[Path] = []
    for category_root in _store_category_roots(root):
        if artifact_id is None:
            paths.extend(
                path
                for path in category_root.glob("*.json")
                if path.is_file() and not path.is_symlink()
            )
        else:
            path = category_root / f"{artifact_id}.json"
            if path.is_file() and not path.is_symlink():
                paths.append(path)
    return sorted(paths)


def _fallback_artifact_paths(root: Path, artifact_id: str) -> list[Path]:
    artifact_id = _validate_artifact_id(artifact_id)
    paths: list[Path] = []
    for category_root in _store_category_roots(root):
        path = category_root / f"{artifact_id}.bin"
        if not path.is_file() or path.is_symlink():
            continue
        try:
            path.resolve(strict=False).relative_to(root)
        except ValueError:
            continue
        paths.append(path)
    return sorted(paths)


def _parse_datetime(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must include timezone")
    return value.astimezone(timezone.utc)
