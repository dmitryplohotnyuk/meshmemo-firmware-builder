import hashlib
import json
import struct
import zlib

import pytest

from meshmemo_builder.cli import main
from meshmemo_builder.pipeline import BuilderError, resolve_plan
from meshmemo_builder.update import image_info, inspect_flash, plan_update


def application(build_id=b"", payload_size=512):
    header = bytearray(24)
    header[0:2] = b"\xe9\x01"
    header[12] = 9
    header[23] = 1
    payload = b"\x32\x54\xcd\xab" + b"\0" * 252 + build_id
    payload = payload.ljust(payload_size, b"\0")
    checksum = 0xEF
    for byte in payload:
        checksum ^= byte
    raw = bytes(header) + struct.pack("<II", 0x3C000020, len(payload)) + payload
    raw += b"\0" * (15 - len(raw) % 16) + bytes([checksum])
    return raw + hashlib.sha256(raw).digest()


def partition(kind, subtype, offset, size, name, flags=0):
    return struct.pack("<HBBII16sI", 0x50AA, kind, subtype, offset, size, name.encode(), flags)


def ota(sequence, state=0xFFFFFFFF):
    raw = struct.pack("<I", sequence) + b"\xff" * 20 + struct.pack("<I", state)
    return raw + struct.pack("<I", zlib.crc32(raw[:4], 0xFFFFFFFF))


def flash_file(tmp_path, *, records=None, slots=(ota(1), ota(0))):
    raw = bytearray(b"\xff" * 0x100000)
    records = records or [partition(1, 2, 0x9000, 0x5000, "nvs"),
                          partition(1, 0, 0xE000, 0x2000, "otadata"),
                          partition(0, 16, 0x10000, 0x20000, "app0"),
                          partition(0, 17, 0x30000, 0x20000, "app1")]
    table = b"".join(records)
    table += b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(table).digest()
    raw[0x8000:0x8000 + len(table)] = table
    for address in (0x10000, 0x30000):
        app = application()
        raw[address:address + len(app)] = app
    for address, entry in zip((0xE000, 0xF000), slots):
        raw[address:address + 32] = entry
    secret = b"PRIVATE_CHANNEL_KEY_DO_NOT_EXPORT!"
    raw[0x9000:0x9000 + len(secret)] = secret
    path = tmp_path / "backup.bin"
    path.write_bytes(raw)
    return path


def build_files(tmp_path, *, board="tbeam-s3-core", payload_size=512):
    plan = resolve_plan(board, windows_workaround=False)
    raw = application(bytes.fromhex(plan["build_id"]), payload_size)
    image = tmp_path / "application.bin"
    image.write_bytes(raw)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(dict(status="built", artifact_kind="application-only",
        plan=plan, artifact=image.name, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw),
        build_partition={"offset": 0x10000, "size": 0x330000})))
    return manifest, image


def mutate(path, offset, value):
    raw = bytearray(path.read_bytes())
    raw[offset:offset + len(value)] = value
    path.write_bytes(raw)


def test_plan_uses_dump_partition_not_build_budget_and_preserves_inputs(tmp_path):
    backup = flash_file(tmp_path)
    manifest, image = build_files(tmp_path)
    original = {p: p.read_bytes() for p in (backup, manifest, image)}
    result = plan_update(backup, manifest, "tbeam-s3-core")
    assert result["status"] == "offline-compatible"
    assert result["proposed_write"]["offset"] == 0x10000
    assert result["proposed_write"]["partition_size_bytes"] == 0x20000
    assert result["proposed_write"]["erase_size_bytes"] == 0x1000
    assert not result["flash_authorized"] and not result["board_identity_verified"]
    assert result["options"] == []
    assert "PRIVATE_CHANNEL" not in json.dumps(result)
    assert all(p.read_bytes() == raw for p, raw in original.items())


def test_larger_image_is_blocked_even_when_build_partition_fits(tmp_path):
    backup = flash_file(tmp_path)
    manifest, _ = build_files(tmp_path, payload_size=0x20000)
    result = plan_update(backup, manifest, "tbeam-s3-core")
    assert result["status"] == "blocked" and result["proposed_write"] is None
    assert "exceeds" in result["blockers"][0]


def test_valid_ota_sequence_selects_second_slot(tmp_path):
    result = inspect_flash(flash_file(tmp_path, slots=(ota(1, 2), ota(2, 2))))
    assert result["selected_application"]["offset"] == 0x30000
    assert not result["blockers"]


@pytest.mark.parametrize("slots", [(ota(1), ota(2, 0)), (ota(1), ota(2, 1)),
    (ota(1), ota(2, 3)), (ota(1), ota(2, 4)), (ota(0), b"\xff" * 32),
    (ota(1), ota(2)[:-1] + b"\0"), (ota(1), ota(0xFFFFFFFE))])
def test_ambiguous_or_damaged_ota_blocks_writing(tmp_path, slots):
    result = inspect_flash(flash_file(tmp_path, slots=slots))
    assert result["blockers"] and result["selected_application"] is None


def test_erased_ota_selects_first_slot(tmp_path):
    result = inspect_flash(flash_file(tmp_path, slots=(b"\xff" * 32,) * 2))
    assert result["selected_application"]["subtype"] == 16


def test_factory_without_ota_supported(tmp_path):
    result = inspect_flash(flash_file(tmp_path, records=[partition(0, 0, 0x10000, 0x20000, "factory")]))
    assert result["selected_application"]["subtype"] == 0 and not result["blockers"]


@pytest.mark.parametrize("records", [
    [partition(0, 16, 0x10000, 0x30000, "app0"), partition(0, 17, 0x30000, 0x20000, "app1")],
    [partition(0, 16, 0x10000, 0x100000, "app0")],
    [partition(0, 16, 0x11000, 0x20000, "app0")],
    [partition(0, 16, 0x10000, 0x20001, "app0")],
    [partition(1, 2, 0x8000, 0x1000, "nvs")],
    [partition(0, 16, 0x10000, 0x20000, "same"), partition(0, 17, 0x30000, 0x20000, "same")],
    [partition(0, 16, 0x10000, 0x20000, "app0"), partition(0, 16, 0x30000, 0x20000, "app1")],
])
def test_invalid_partition_ranges_are_rejected(tmp_path, records):
    with pytest.raises(BuilderError):
        inspect_flash(flash_file(tmp_path, records=records))


def test_partition_md5_and_missing_checksum_rejected(tmp_path):
    backup = flash_file(tmp_path)
    mutate(backup, 0x8000 + 12, b"X")
    with pytest.raises(BuilderError, match="MD5"):
        inspect_flash(backup)
    backup = flash_file(tmp_path)
    mutate(backup, 0x8000 + 4 * 32, b"\xff" * 32)
    with pytest.raises(BuilderError, match="missing MD5"):
        inspect_flash(backup)


def test_flags_block_plan_and_do_not_assume_encryption_state(tmp_path):
    result = inspect_flash(flash_file(tmp_path, records=[partition(0, 0, 0x10000, 0x20000, "factory", 1)]))
    assert any("flags/encryption" in reason for reason in result["blockers"])


def test_damaged_selected_image_never_guesses_fallback(tmp_path):
    backup = flash_file(tmp_path)
    mutate(backup, 0x10000, b"\0")
    result = inspect_flash(backup)
    assert result["blockers"] and result["installed_image"] is None
    assert result["selected_application"]["offset"] == 0x10000


@pytest.mark.parametrize("offset,value", [(0, b"\0"), (1, b"\x11"), (12, b"\x02"),
    (23, b"\0"), (28, b"\xff\xff\xff\x7f"), (32, b"\0"), (300, b"\x01"), (-1, b"\0")])
def test_image_header_segment_checksum_and_digest_validation(offset, value):
    raw = bytearray(application())
    if offset == -1:
        raw[-1] ^= 1
    else:
        raw[offset:offset + len(value)] = value
    with pytest.raises(BuilderError):
        image_info(bytes(raw), exact=True)


@pytest.mark.parametrize("raw", [b"", b"\xe9", application()[:-20], application() + b"signature"])
def test_truncation_and_trailing_signature_rejected(raw):
    with pytest.raises(BuilderError):
        image_info(raw, exact=True)


def test_heltec_candidate_cannot_be_selected_as_tbeam(tmp_path):
    manifest, _ = build_files(tmp_path, board="heltec-v3")
    with pytest.raises(BuilderError, match="board"):
        plan_update(flash_file(tmp_path), manifest, "tbeam-s3-core")


@pytest.mark.parametrize("change", ["hash", "path", "profile", "identity", "factory", "not-built", "model"])
def test_modified_manifest_rejected(tmp_path, change):
    manifest, _ = build_files(tmp_path)
    report = json.loads(manifest.read_text())
    if change == "hash": report["sha256"] = "0" * 64
    if change == "path": report["artifact"] = "../outside.bin"
    if change == "profile": report["plan"]["profile"] = "unknown"
    if change == "identity": report["plan"]["build_id"] = "0" * 64
    if change == "factory": report["artifact_kind"] = "factory"
    if change == "not-built": report["status"] = "failed"
    if change == "model": report["plan"]["hardware"]["hardware_model"] = 43
    manifest.write_text(json.dumps(report))
    with pytest.raises(BuilderError):
        plan_update(flash_file(tmp_path), manifest, "tbeam-s3-core")


def test_cli_json_exit_codes_and_no_overwrite(tmp_path, capsys):
    backup = flash_file(tmp_path)
    manifest, image = build_files(tmp_path)
    args = ["plan-update", "--backup", str(backup), "--manifest", str(manifest), "--board", "tbeam-s3-core"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "offline-compatible"
    image_before = image.read_bytes()
    assert main(args + ["--output", str(image)]) == 1
    assert image.read_bytes() == image_before
    mutate(backup, 0xE000, b"\0\0\0\0")
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
    manifest.write_text("{")
    assert main(args) == 1
    assert "Traceback" not in capsys.readouterr().err


def test_partial_backup_rejected(tmp_path):
    backup = tmp_path / "partial.bin"
    backup.write_bytes(b"\xff" * 4096)
    with pytest.raises(BuilderError, match="complete"):
        inspect_flash(backup)


def test_image_with_matching_hash_but_wrong_embedded_identity_rejected(tmp_path):
    manifest, image = build_files(tmp_path)
    image.write_bytes(application())
    report = json.loads(manifest.read_text())
    report.update(sha256=hashlib.sha256(image.read_bytes()).hexdigest(), size_bytes=image.stat().st_size)
    manifest.write_text(json.dumps(report))
    with pytest.raises(BuilderError, match="build ID"):
        plan_update(flash_file(tmp_path), manifest, "tbeam-s3-core")


@pytest.mark.parametrize("value", [None, [], {}, {"status": "built", "artifact_kind": "application-only", "plan": []}])
def test_malformed_json_structure_has_actionable_error(tmp_path, value):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(value))
    with pytest.raises(BuilderError, match="manifest"):
        plan_update(flash_file(tmp_path), manifest, "tbeam-s3-core")


@pytest.mark.parametrize("board,model", [("heltec-v3", 43), ("heltec-wsl-v3", 44),
    ("heltec-wireless-tracker", 48), ("tlora-t3s3-v1", 16)])
def test_offline_plan_needs_no_subprocess_or_network(tmp_path, monkeypatch, board, model):
    import socket
    import subprocess
    def forbidden(*args, **kwargs):
        pytest.fail("Offline planner attempted external access")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    manifest, _ = build_files(tmp_path, board=board)
    result = plan_update(flash_file(tmp_path), manifest, board)
    assert result["status"] == "offline-compatible" and result["hardware_model"] == model
