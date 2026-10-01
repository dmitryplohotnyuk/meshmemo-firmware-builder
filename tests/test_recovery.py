import json
from pathlib import Path
import socket
import subprocess

import pytest

from meshmemo_builder.cli import main
from meshmemo_builder.pipeline import BuilderError
from meshmemo_builder.recovery import FILES, prepare_recovery, verify_recovery
from meshmemo_builder.update import digest
from test_update import application, flash_file, mutate, ota, partition


def test_self_contained_package_copies_exact_bytes_and_excludes_unrelated_files(tmp_path):
    backup = flash_file(tmp_path)
    original = backup.read_bytes()
    (tmp_path / "server-password.txt").write_text("NOT_PART_OF_THE_PACKAGE")
    destination = tmp_path / "recovery"
    manifest_path = prepare_recovery(backup, destination)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert {p.name for p in destination.iterdir()} == FILES
    assert (destination / "flash-backup.bin").read_bytes() == original
    assert (destination / "rollback-app.bin").read_bytes() == application()
    assert backup.read_bytes() == original
    assert manifest["application"]["offset"] == 0x10000
    assert manifest["contains_private_data"] and not manifest["flash_authorized"]
    assert "PRIVATE_CHANNEL" not in manifest_path.read_text(encoding="utf-8")
    assert "NOT_PART_OF_THE_PACKAGE" not in repr([p.read_bytes() for p in destination.iterdir()])
    backup.unlink()  # Verification is independent of the original file/location.
    moved = tmp_path / "moved-recovery"
    destination.rename(moved)
    result = verify_recovery(moved)
    assert result["status"] == "verified" and result["files_verified"] == 5
    assert not result["device_accessed"] and not result["flash_authorized"]


def test_second_ota_slot_is_extracted_without_hardcoded_offset(tmp_path):
    backup = flash_file(tmp_path, slots=(ota(1, 2), ota(2, 2)))
    other = application(b"second-slot".ljust(32, b"\0"))
    mutate(backup, 0x30000, other)
    destination = tmp_path / "recovery"
    prepare_recovery(backup, destination)
    assert (destination / "rollback-app.bin").read_bytes() == other
    assert verify_recovery(destination)["application"]["offset"] == 0x30000


def test_factory_image_can_be_extracted(tmp_path):
    backup = flash_file(tmp_path, records=[partition(0, 0, 0x10000, 0x20000, "factory")])
    destination = tmp_path / "recovery"
    prepare_recovery(backup, destination)
    assert verify_recovery(destination)["application"]["sha256"] == digest(application())


@pytest.mark.parametrize("mode", ["bad-md5", "bad-app", "pending-ota", "encrypted", "partial"])
def test_invalid_backup_never_creates_destination(tmp_path, mode):
    backup = flash_file(tmp_path)
    if mode == "bad-md5": mutate(backup, 0x8000 + 12, b"X")
    if mode == "bad-app": mutate(backup, 0x10000, b"\0")
    if mode == "pending-ota": mutate(backup, 0xE000, ota(1, 1))
    if mode == "encrypted":
        backup = flash_file(tmp_path, records=[partition(0, 0, 0x10000, 0x20000, "factory", 1)])
    if mode == "partial": backup.write_bytes(b"short")
    destination = tmp_path / "recovery"
    original = backup.read_bytes()
    with pytest.raises(BuilderError):
        prepare_recovery(backup, destination)
    assert not destination.exists() and backup.read_bytes() == original


@pytest.mark.parametrize("existing", ["empty-directory", "directory-with-files", "file", "input-file"])
def test_existing_destination_is_never_reused(tmp_path, existing):
    backup = flash_file(tmp_path)
    destination = tmp_path / "recovery"
    if existing == "input-file": destination = backup
    elif existing == "file": destination.write_bytes(b"keep")
    else:
        destination.mkdir()
        if existing == "directory-with-files": (destination / "keep.txt").write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        prepare_recovery(backup, destination)
    if existing == "empty-directory": assert not list(destination.iterdir())
    if existing == "directory-with-files": assert (destination / "keep.txt").read_bytes() == b"keep"
    if existing == "file": assert destination.read_bytes() == b"keep"
    if existing == "input-file": assert len(backup.read_bytes()) == 0x100000


@pytest.mark.parametrize("filename", sorted(FILES))
def test_modified_package_file_is_rejected(tmp_path, filename):
    destination = tmp_path / "recovery"
    prepare_recovery(flash_file(tmp_path), destination)
    path = destination / filename
    # For flash this mutates NVS, not an app/table: the backup hash must still catch it.
    mutate(path, 0x9000 if filename == "flash-backup.bin" else 0, b"X")
    with pytest.raises(BuilderError):
        verify_recovery(destination)


def test_rehashed_manifest_cannot_change_restore_offset(tmp_path):
    destination = tmp_path / "recovery"
    prepare_recovery(flash_file(tmp_path), destination)
    path = destination / "recovery.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report["application"]["offset"] = 0
    report["files"]["rollback-app.bin"]["path"] = "../outside.bin"
    path.write_text(json.dumps(report), encoding="utf-8")
    sums = "".join(f"{digest((destination / name).read_bytes())}  {name}\n" for name in sorted(FILES - {"SHA256SUMS"}))
    (destination / "SHA256SUMS").write_text(sums, encoding="ascii")
    with pytest.raises(BuilderError, match="recovery.json"):
        verify_recovery(destination)


@pytest.mark.parametrize("mode", ["missing", "extra", "incomplete", "directory"])
def test_incomplete_or_unexpected_contents_rejected(tmp_path, mode):
    destination = tmp_path / "recovery"
    prepare_recovery(flash_file(tmp_path), destination)
    if mode == "missing": (destination / "rollback-app.bin").unlink()
    if mode == "extra": (destination / "unexpected.bin").write_bytes(b"extra")
    if mode == "incomplete": (destination / ".incomplete").write_bytes(b"interrupted")
    if mode == "directory":
        (destination / "rollback-app.bin").unlink()
        (destination / "rollback-app.bin").mkdir()
    with pytest.raises(BuilderError):
        verify_recovery(destination)


def test_write_failure_leaves_marker_and_original_backup_intact(tmp_path, monkeypatch):
    backup = flash_file(tmp_path)
    original = backup.read_bytes()
    destination = tmp_path / "recovery"
    real_open = Path.open
    def fail_write(path, *args, **kwargs):
        if path.name == "rollback-app.bin" and args == ("xb",):
            raise OSError("simulated disk full")
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", fail_write)
    with pytest.raises(OSError, match="disk full"):
        prepare_recovery(backup, destination)
    assert (destination / ".incomplete").exists()
    assert backup.read_bytes() == original
    with pytest.raises(BuilderError, match="Incomplete"):
        verify_recovery(destination)


def test_source_is_read_once_and_package_is_deterministic(tmp_path, monkeypatch):
    backup = flash_file(tmp_path)
    real_open = Path.open
    reads = []
    def track(path, *args, **kwargs):
        if path == backup and args == ("rb",): reads.append(path)
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", track)
    one, two = tmp_path / "one", tmp_path / "two"
    prepare_recovery(backup, one)
    assert len(reads) == 1
    prepare_recovery(backup, two)
    assert all((one / name).read_bytes() == (two / name).read_bytes() for name in FILES)


def test_no_external_access_and_cli_errors(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs): pytest.fail("Unexpected external access")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    backup = flash_file(tmp_path)
    destination = tmp_path / "recovery"
    args = ["prepare-recovery", "--backup", str(backup), "--destination", str(destination)]
    assert main(args) == 0
    capsys.readouterr()
    assert main(["verify-recovery", "--package", str(destination)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "verified"
    assert main(args) == 1
    (destination / "SHA256SUMS").unlink()
    assert main(["verify-recovery", "--package", str(destination)]) == 1
    assert "Traceback" not in capsys.readouterr().err
