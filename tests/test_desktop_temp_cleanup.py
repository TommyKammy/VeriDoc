from __future__ import annotations

import logging

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
