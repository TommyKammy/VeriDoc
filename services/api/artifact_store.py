from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from os import PathLike
from pathlib import Path
import re
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARTIFACT_STORE_ROOT = REPO_ROOT / "var" / "veridoc" / "artifacts"
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ArtifactFileRecord:
    content_sha256: str
    size_bytes: int
    created: bool


class ArtifactFileStore:
    """Content-addressed durable storage for job artifact bytes."""

    def __init__(self, root: str | PathLike[str]) -> None:
        self._root = _validate_artifact_store_root(Path(root))

    @property
    def root(self) -> Path:
        return self._root

    def save(self, content: bytes) -> ArtifactFileRecord:
        if not isinstance(content, bytes):
            raise ValueError("artifact content must be bytes")
        content_sha256 = sha256(content).hexdigest()
        path = self._path(content_sha256, create_parent=True)
        if path.exists():
            self._verify_path(path, content_sha256, len(content))
            return ArtifactFileRecord(content_sha256, len(content), False)

        temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary_path.open("xb") as artifact_file:
                artifact_file.write(content)
                artifact_file.flush()
                os.fsync(artifact_file.fileno())
            temporary_path.replace(path)
            self._verify_path(path, content_sha256, len(content))
        finally:
            temporary_path.unlink(missing_ok=True)
        return ArtifactFileRecord(content_sha256, len(content), True)

    def read(self, content_sha256: str, *, size_bytes: int) -> bytes:
        path = self._path(content_sha256)
        return self._read_verified(path, content_sha256, size_bytes)

    def delete(self, content_sha256: str) -> None:
        self._path(content_sha256).unlink(missing_ok=True)

    def _path(self, content_sha256: str, *, create_parent: bool = False) -> Path:
        if not isinstance(content_sha256, str) or not _SHA256_HEX.fullmatch(
            content_sha256
        ):
            raise ValueError("artifact content hash is invalid")
        if create_parent:
            self._ensure_root()
        digest_root = self._root / content_sha256[:2]
        if create_parent:
            if digest_root.is_symlink():
                raise ValueError("artifact content directory is invalid")
            digest_root.mkdir(exist_ok=True)
        if digest_root.is_symlink():
            raise ValueError("artifact content directory is invalid")
        return digest_root / f"{content_sha256}.bin"

    def _ensure_root(self) -> None:
        if self._root.is_symlink():
            raise ValueError("artifact store root is invalid")
        self._root.mkdir(parents=True, exist_ok=True)
        if self._root.is_symlink() or not self._root.is_dir():
            raise ValueError("artifact store root is invalid")

    @staticmethod
    def _verify_path(path: Path, content_sha256: str, size_bytes: int) -> None:
        ArtifactFileStore._read_verified(path, content_sha256, size_bytes)

    @staticmethod
    def _read_verified(path: Path, content_sha256: str, size_bytes: int) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise ValueError("persisted job artifact is missing")
        content = path.read_bytes()
        if len(content) != size_bytes or sha256(content).hexdigest() != content_sha256:
            raise ValueError("persisted job artifact integrity check failed")
        return content


def default_artifact_store_root() -> Path:
    configured = os.environ.get("VERIDOC_ARTIFACT_STORE_ROOT")
    artifact_root = Path(configured) if configured else DEFAULT_ARTIFACT_STORE_ROOT
    return _validate_artifact_store_root(artifact_root)


def _validate_artifact_store_root(root: Path) -> Path:
    if str(root).strip() == "":
        raise ValueError("artifact store root is required")
    if not root.is_absolute():
        repo_root = REPO_ROOT.resolve()
        resolved_root = (repo_root / root).resolve()
        if resolved_root != repo_root and repo_root not in resolved_root.parents:
            raise ValueError("relative artifact store root must stay within the repository root")
        root = resolved_root
    else:
        root = root.resolve(strict=False)
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise ValueError("artifact store root must be a directory")
    return root


__all__ = [
    "ArtifactFileRecord",
    "ArtifactFileStore",
    "DEFAULT_ARTIFACT_STORE_ROOT",
    "default_artifact_store_root",
]
