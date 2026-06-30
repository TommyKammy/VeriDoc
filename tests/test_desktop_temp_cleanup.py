from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from apps.desktop.api_client import (
    DesktopTemporaryCleanupError,
    DesktopTemporaryFileManager,
)


def test_desktop_temporary_files_are_removed_after_success_failure_and_cancel(
    tmp_path,
) -> None:
    temp_root = tmp_path / "desktop-temp"

    with DesktopTemporaryFileManager(temp_root) as manager:
        success_temp = manager.create_staging_file("upload.pdf", b"source")
        explicit_save = tmp_path / "selected-output" / "result.json"
        explicit_save.parent.mkdir()
        explicit_save.write_bytes(b"saved by operator")
        manager.register_explicit_artifact(explicit_save)

    assert not success_temp.exists()
    assert explicit_save.read_bytes() == b"saved by operator"

    with pytest.raises(RuntimeError, match="simulated conversion failure"):
        with DesktopTemporaryFileManager(temp_root) as manager:
            failure_temp = manager.create_staging_file("intermediate.json", b"partial")
            raise RuntimeError("simulated conversion failure")

    assert not failure_temp.exists()

    manager = DesktopTemporaryFileManager(temp_root)
    cancel_temp = manager.create_staging_file("cancelled.docx", b"cancelled")
    manager.cancel()

    assert not cancel_temp.exists()


def test_desktop_temporary_cleanup_failure_is_logged_and_raised(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = DesktopTemporaryFileManager(tmp_path / "desktop-temp")
    temp_file = manager.create_staging_file("upload.pdf", b"source")
    original_unlink = type(temp_file).unlink

    def failing_unlink(path, *args, **kwargs):
        if path == temp_file:
            raise OSError("simulated cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(temp_file), "unlink", failing_unlink)
    caplog.set_level(logging.ERROR, logger="apps.desktop.api_client")

    with pytest.raises(DesktopTemporaryCleanupError, match="temporary cleanup failed"):
        manager.cleanup()

    assert temp_file.exists()
    assert "temporary cleanup failed" in caplog.text


def test_desktop_staging_filename_reserves_temp_prefix_bytes(tmp_path) -> None:
    manager = DesktopTemporaryFileManager(tmp_path / "desktop-temp")

    temp_file = manager.create_staging_file(f"{'a' * 300}.json", b"source")

    assert temp_file.name.startswith("tmp-")
    assert len(temp_file.name.encode("utf-8")) <= 255


def test_desktop_tracks_staging_file_before_write_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_root = tmp_path / "desktop-temp"
    manager = DesktopTemporaryFileManager(temp_root)
    original_open = Path.open

    class FailingWriter:
        def __init__(self, path: Path, mode: str, args: tuple[object, ...], kwargs: dict[str, object]) -> None:
            self._path = path
            self._mode = mode
            self._args = args
            self._kwargs = kwargs
            self._file = None

        def __enter__(self) -> "FailingWriter":
            self._file = original_open(self._path, self._mode, *self._args, **self._kwargs)
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            if self._file is not None:
                self._file.close()

        def write(self, content: bytes) -> None:
            assert self._file is not None
            self._file.write(content[:1])
            raise OSError("simulated partial write failure")

    def failing_open(path: Path, mode: str = "r", *args: object, **kwargs: object):
        if mode == "xb" and path.parent == temp_root / "work":
            return FailingWriter(path, mode, args, kwargs)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)

    with pytest.raises(OSError, match="simulated partial write failure"):
        manager.create_staging_file("partial.json", b"source")

    partials = list((temp_root / "work").iterdir())
    assert len(partials) == 1

    manager.cleanup()

    assert not partials[0].exists()


def test_desktop_cleanup_unlinks_replaced_symlink_without_touching_target(tmp_path) -> None:
    temp_root = tmp_path / "desktop-temp"
    target = tmp_path / "operator-output.json"
    target.write_bytes(b"explicit saved output")
    manager = DesktopTemporaryFileManager(temp_root)
    temp_file = manager.create_staging_file("intermediate.json", b"temporary")
    temp_file.unlink()
    temp_file.symlink_to(target)

    manager.cleanup()

    assert not os.path.lexists(temp_file)
    assert target.read_bytes() == b"explicit saved output"


def test_desktop_rejects_symlinked_work_dir_before_staging(tmp_path) -> None:
    temp_root = tmp_path / "desktop-temp"
    outside = tmp_path / "outside"
    temp_root.mkdir()
    outside.mkdir()
    (temp_root / "work").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="temporary work directory must not be a symlink"):
        DesktopTemporaryFileManager(temp_root)


def test_desktop_cleanup_rejects_replaced_symlinked_work_dir(tmp_path) -> None:
    temp_root = tmp_path / "desktop-temp"
    outside = tmp_path / "outside"
    outside.mkdir()
    manager = DesktopTemporaryFileManager(temp_root)
    temp_file = manager.create_staging_file("intermediate.json", b"temporary")
    temp_file.unlink()
    (temp_root / "work").rmdir()
    outside_target = outside / temp_file.name
    outside_target.write_bytes(b"outside file")
    (temp_root / "work").symlink_to(outside, target_is_directory=True)

    with pytest.raises(DesktopTemporaryCleanupError, match="temporary cleanup failed"):
        manager.cleanup()

    assert outside_target.read_bytes() == b"outside file"
