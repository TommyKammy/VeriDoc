from __future__ import annotations

import logging
import os
import stat
import subprocess
from pathlib import Path

import pytest

from apps.desktop import api_client
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not stable on Windows")
def test_desktop_temporary_files_and_work_dir_are_private_with_common_umask(tmp_path) -> None:
    temp_root = tmp_path / "desktop-temp"
    old_umask = os.umask(0o022)
    try:
        manager = DesktopTemporaryFileManager(temp_root)
        temp_file = manager.create_staging_file("source document.pdf", b"source")
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(temp_root.stat().st_mode) == 0o700
    assert stat.S_IMODE((temp_root / "work").stat().st_mode) == 0o700
    assert stat.S_IMODE(temp_file.stat().st_mode) == 0o600


def test_desktop_windows_acl_hardening_runs_for_private_temp_paths(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_root = tmp_path / "desktop-temp"
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs["env"]))  # type: ignore[arg-type]
        assert kwargs["check"] is True
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.PIPE
        assert kwargs["text"] is True
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(api_client.sys, "platform", "win32")
    monkeypatch.setattr(api_client.subprocess, "run", fake_run)

    manager = DesktopTemporaryFileManager(temp_root)
    temp_file = manager.create_staging_file("source document.pdf", b"source")

    assert [Path(env["VERIDOC_PRIVATE_ACL_PATH"]) for _, env in calls] == [
        temp_root,
        temp_root / "work",
        temp_file,
    ]
    for command, _ in calls:
        assert command[:5] == [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
        ]
        assert "SetAccessRuleProtection($true, $false)" in command[-1]
        assert "RemoveAccessRuleAll" in command[-1]
    assert temp_file.read_bytes() == b"source"


def test_desktop_windows_acl_failure_removes_untracked_staging_file(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_root = tmp_path / "desktop-temp"

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        secured_path = Path(kwargs["env"]["VERIDOC_PRIVATE_ACL_PATH"])  # type: ignore[index]
        if secured_path.name.startswith("tmp-"):
            raise subprocess.CalledProcessError(1, command, stderr="denied")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(api_client.sys, "platform", "win32")
    monkeypatch.setattr(api_client.subprocess, "run", fake_run)

    manager = DesktopTemporaryFileManager(temp_root)
    with pytest.raises(OSError, match="failed to secure Windows temporary path ACL"):
        manager.create_staging_file("source document.pdf", b"source")

    assert list((temp_root / "work").iterdir()) == []


def test_desktop_cleanup_keeps_shared_work_dir_available(tmp_path) -> None:
    temp_root = tmp_path / "desktop-temp"
    first = DesktopTemporaryFileManager(temp_root)
    second = DesktopTemporaryFileManager(temp_root)

    first_temp = first.create_staging_file("first.json", b"first")
    first.cleanup()
    second_temp = second.create_staging_file("second.json", b"second")

    assert not first_temp.exists()
    assert second_temp.read_bytes() == b"second"
    assert (temp_root / "work").is_dir()

    second.cleanup()

    assert not second_temp.exists()
    assert (temp_root / "work").is_dir()


def test_desktop_tracks_staging_file_before_write_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_root = tmp_path / "desktop-temp"
    manager = DesktopTemporaryFileManager(temp_root)
    original_fdopen = os.fdopen

    class FailingWriter:
        def __init__(self, file_obj) -> None:
            self._file = file_obj

        def __enter__(self) -> "FailingWriter":
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            self._file.close()

        def write(self, content: bytes) -> None:
            self._file.write(content[:1])
            raise OSError("simulated partial write failure")

    def failing_fdopen(fd: int, mode: str = "r", *args: object, **kwargs: object):
        file_obj = original_fdopen(fd, mode, *args, **kwargs)
        if mode == "wb":
            return FailingWriter(file_obj)
        return file_obj

    monkeypatch.setattr(os, "fdopen", failing_fdopen)

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
