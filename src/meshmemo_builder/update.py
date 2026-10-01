"""Offline ESP32-S3 backup inspection and application-only update planning.

No serial, network or flashing dependencies. A plan is evidence about files,
never authorization to write a connected device.
"""

import hashlib
import json
from pathlib import Path
import struct
import zlib

from .pipeline import BuilderError, contained, resolve_plan

SECTOR = 0x1000
TABLE = 0x8000
MAX_FLASH = 32 * 1024 * 1024


def read_binary(path: Path, maximum=MAX_FLASH) -> bytes:
    if not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise BuilderError(f"Expected a regular nonempty file of at most {maximum} bytes: {path}")
    with path.open("rb") as source:
        raw = source.read(maximum + 1)
    if not 0 < len(raw) <= maximum:
        raise BuilderError("Input changed size while reading")
    return raw


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def image_info(raw: bytes, *, exact=False) -> dict:
    if len(raw) < 32 or raw[0] != 0xE9 or not 1 <= raw[1] <= 16:
        raise BuilderError("Invalid ESP application header (encrypted images are unsupported)")
    if struct.unpack_from("<H", raw, 12)[0] != 9:
        raise BuilderError("Only ESP32-S3 application images are supported")
    if raw[23] != 1:
        raise BuilderError("Application must include its SHA-256 digest")
    cursor, checksum = 24, 0xEF
    for index in range(raw[1]):
        if cursor + 8 > len(raw):
            raise BuilderError("Truncated application segment header")
        _, size = struct.unpack_from("<II", raw, cursor)
        cursor += 8
        if size % 4 or cursor + size > len(raw):
            raise BuilderError("Invalid application segment length")
        if index == 0 and (size < 256 or raw[cursor:cursor + 4] != b"\x32\x54\xcd\xab"):
            raise BuilderError("Missing ESP application descriptor; bootloader/factory files are unsupported")
        for byte in raw[cursor:cursor + size]:
            checksum ^= byte
        cursor += size
    checksum_at = cursor + (15 - cursor % 16)
    end = checksum_at + 1
    if end + 32 > len(raw):
        raise BuilderError("Truncated application checksum/digest")
    if any(raw[cursor:checksum_at]) or raw[checksum_at] != checksum:
        raise BuilderError("Application checksum or padding mismatch")
    if hashlib.sha256(raw[:end]).digest() != raw[end:end + 32]:
        raise BuilderError("Application SHA-256 digest mismatch")
    size = end + 32
    if exact and size != len(raw):
        raise BuilderError("Trailing application data/signatures are unsupported")
    return {"chip": "esp32-s3", "size_bytes": size, "sha256": digest(raw[:size]),
            "checksum_verified": True, "digest_verified": True}


def partitions(raw: bytes) -> list[dict]:
    table = raw[TABLE:TABLE + SECTOR]
    parts = []
    for at in range(0, 0xC00, 32):
        record = table[at:at + 32]
        if record[:16] == b"\xeb\xeb" + b"\xff" * 14:
            if hashlib.md5(table[:at]).digest() != record[16:]:
                raise BuilderError("Partition table MD5 mismatch")
            if any(byte != 0xFF for byte in table[at + 32:]):
                raise BuilderError("Unexpected data after partition table checksum")
            break
        if len(record) != 32 or record[:2] != b"\xaaP":
            raise BuilderError("Invalid partition table or missing MD5 record at 0x8000")
        _, kind, subtype, offset, size, label, flags = struct.unpack("<HBBII16sI", record)
        try:
            name = label.split(b"\0", 1)[0].decode("ascii")
        except UnicodeDecodeError as exc:
            raise BuilderError("Invalid partition label") from exc
        if not name or not all(32 <= ord(char) < 127 for char in name):
            raise BuilderError("Invalid partition label")
        if (not size or offset < TABLE + SECTOR or offset % SECTOR or size % SECTOR
                or offset + size > len(raw) or (kind == 0 and offset % 0x10000)):
            raise BuilderError(f"Invalid partition bounds/alignment: {name}")
        if any(p["name"] == name for p in parts):
            raise BuilderError("Duplicate partition label")
        parts.append(dict(name=name, type=kind, subtype=subtype, offset=offset, size=size, flags=flags))
    else:
        raise BuilderError("Missing partition table MD5 record")
    ordered = sorted(parts, key=lambda p: p["offset"])
    if not parts or any(a["offset"] + a["size"] > b["offset"] for a, b in zip(ordered, ordered[1:])):
        raise BuilderError("Empty or overlapping partition table")
    apps = [p for p in parts if p["type"] == 0]
    if len({p["subtype"] for p in apps}) != len(apps):
        raise BuilderError("Duplicate application subtype")
    return parts


def selected_app(raw: bytes, parts: list[dict]) -> tuple[dict | None, str]:
    apps = [p for p in parts if p["type"] == 0]
    factory = next((p for p in apps if p["subtype"] == 0), None)
    ota_apps = sorted((p for p in apps if 0x10 <= p["subtype"] <= 0x1F), key=lambda p: p["subtype"])
    if any(p["subtype"] not in (0, *range(0x10, 0x20)) for p in apps):
        return None, "Unsupported application subtype"
    if [p["subtype"] for p in ota_apps] != list(range(0x10, 0x10 + len(ota_apps))):
        return None, "OTA application slots are not contiguous"
    metadata = [p for p in parts if (p["type"], p["subtype"]) == (1, 0)]
    if not metadata:
        return (factory, "Factory application without OTA metadata") if factory else (None, "Missing OTA metadata")
    if len(metadata) != 1 or metadata[0]["size"] != 2 * SECTOR or metadata[0]["flags"]:
        return None, "Unsupported OTA metadata partition"
    sequences = []
    for offset in (metadata[0]["offset"], metadata[0]["offset"] + SECTOR):
        entry = raw[offset:offset + 32]
        if entry == b"\xff" * 32:
            continue
        sequence = struct.unpack_from("<I", entry)[0]
        state, crc = struct.unpack_from("<II", entry, 24)
        if sequence == 0xFFFFFFFF or crc != zlib.crc32(entry[:4], 0xFFFFFFFF):
            return None, "Invalid OTA sequence/CRC; bootloader fallback is not inferred"
        if state not in (2, 0xFFFFFFFF):
            return None, "OTA validation/rollback state requires a live device check"
        sequences.append(sequence)
    if not sequences:
        return factory or next(iter(ota_apps), None), "Erased OTA metadata: factory or first OTA slot"
    if not ota_apps:
        return None, "OTA metadata exists without OTA applications"
    if max(sequences) == 0:
        return None, "OTA sequence zero alone does not identify a supported application"
    if len(sequences) == 2 and abs(sequences[0] - sequences[1]) > 0x7FFFFFFF:
        return None, "Ambiguous OTA sequence wraparound"
    return ota_apps[(max(sequences) - 1) % len(ota_apps)], "Selected by stable OTA sequence in backup"


def inspect_flash(path: Path) -> dict:
    return inspect_flash_bytes(read_binary(path))


def inspect_flash_bytes(raw: bytes) -> dict:
    """Inspect the same immutable bytes subsequently used for extraction."""
    if not 1024 * 1024 <= len(raw) <= MAX_FLASH or len(raw) & (len(raw) - 1):
        raise BuilderError("Expected a complete power-of-two flash dump (1–32 MiB), starting at address zero")
    parts = partitions(raw)
    app, reason = selected_app(raw, parts)
    blockers = []
    if any(p["flags"] for p in parts):
        blockers.append("Partition flags/encryption are unsupported for offline update planning")
    installed = None
    if app is None:
        blockers.append(reason)
    else:
        try:
            installed = image_info(raw[app["offset"]:app["offset"] + app["size"]])
        except BuilderError as exc:
            blockers.append(f"Selected application: {exc}")
    return {"schema_version": 1, "kind": "flash-inspection", "evidence": "backup-file-only",
            "flash_size_bytes": len(raw), "backup_sha256": digest(raw),
            "partition_table": {"offset": TABLE, "md5_verified": True}, "partitions": parts,
            "selected_application": app, "selection_reason": reason, "installed_image": installed,
            "blockers": blockers, "device_accessed": False, "board_identity_verified": False}


def candidate(manifest: Path, board: str) -> tuple[dict, dict]:
    try:
        report = json.loads(read_binary(manifest, 1024 * 1024))
        if report["status"] != "built" or report["artifact_kind"] != "application-only":
            raise BuilderError("Requires a completed application-only build manifest")
        plan = report["plan"]
        if plan["board"] != board:
            raise BuilderError("Selected board does not match the build manifest")
        if not isinstance(plan["options"], list) or type(plan["windows_workaround"]) is not bool:
            raise BuilderError("Invalid build options")
        expected = resolve_plan(board, plan["upstream"], plan["profile"], plan["options"], plan["windows_workaround"])
        def inputs(value):
            return {**value, "hardware": {k: v for k, v in value["hardware"].items() if k != "status"}}
        if inputs(plan) != inputs(expected):
            raise BuilderError("Build plan/build ID differs from the installed registry")
        name = report["artifact"]
        if not isinstance(name, str) or Path(name).name != name:
            raise BuilderError("Application artifact must be a filename beside its manifest")
        raw = read_binary(contained(manifest.parent, name))
        if type(report["size_bytes"]) is not int or len(raw) != report["size_bytes"] or digest(raw) != report["sha256"]:
            raise BuilderError("Application does not match manifest size/SHA-256")
        info = image_info(raw, exact=True)
        if bytes.fromhex(plan["build_id"]) not in raw[32:info["size_bytes"] - 32]:
            raise BuilderError("Application does not contain the expected MeshMemo build ID")
        return report, info
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise BuilderError(f"Invalid build manifest: {exc}") from exc


def plan_update(backup: Path, manifest: Path, board: str) -> dict:
    report, new_image = candidate(manifest, board)
    inspection = inspect_flash(backup)
    app = inspection["selected_application"]
    blockers = list(inspection["blockers"])
    erased = ((new_image["size_bytes"] + SECTOR - 1) // SECTOR) * SECTOR
    if app and erased > app["size"]:
        blockers.append("Application/erase range exceeds the selected partition in the backup")
    plan = report["plan"]
    return {"schema_version": 1, "kind": "update-plan", "mode": "offline-app-only",
            "status": "blocked" if blockers else "offline-compatible",
            "flash_authorized": False, "device_accessed": False,
            "board": board, "hardware_model": plan["hardware"]["hardware_model"],
            "board_identity_verified": False, "options": plan["options"], "build_id": plan["build_id"],
            "candidate": new_image, "backup": inspection, "blockers": blockers,
            "proposed_write": None if blockers else {"offset": app["offset"],
                "size_bytes": new_image["size_bytes"], "erase_size_bytes": erased,
                "partition_size_bytes": app["size"], "remaining_bytes": app["size"] - new_image["size_bytes"],
                "preserve": ["bootloader", "partition-table", "OTA-metadata", "other-partitions"]},
            "rollback": {"source": "original-full-backup", "backup_sha256": inspection["backup_sha256"],
                         "application": inspection["installed_image"]},
            "required_live_checks": ["Device board/identity and backup provenance/freshness",
                "Flash capacity, chip revision, eFuses: secure boot, encryption and anti-rollback",
                "Running application and current partition/OTA metadata match this backup",
                "Exclusive serial access and a separate configuration/channel backup",
                "After writing: hash, boot, configuration/channels and MeshMemo capabilities"],
            "limitations": ["Backup metadata cannot prove which application is running on a device",
                "Hashes verify file consistency, not publisher authenticity",
                "Application-only writes preserve data partitions but new firmware may migrate their contents"]}
