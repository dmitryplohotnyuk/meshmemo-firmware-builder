"""Create and verify self-contained recovery files without accessing hardware."""

import json
from pathlib import Path

from .pipeline import BuilderError
from .update import SECTOR, digest, image_info, inspect_flash_bytes, read_binary

FILES = {"flash-backup.bin", "rollback-app.bin", "recovery.json", "README.md", "SHA256SUMS"}
# Schema 1 has canonical metadata/instructions. Keep this layout stable when
# adding later formats so previously generated packages remain verifiable.
INSTRUCTIONS = """# Пакет восстановления MeshMemo

Этот пакет подготовлен из сохранённой flash-копии. Устройство не проверялось.
Создание и проверка пакета ничего не прошивают.

## Содержимое

- flash-backup.bin — полная копия исходной flash, включая настройки и ключи.
- rollback-app.bin — проверенное приложение из выбранного в копии раздела.
- recovery.json — разметка, адрес, размеры, хеши и ограничения восстановления.
- SHA256SUMS — контрольные суммы четырёх остальных файлов.

Пакет содержит личные данные и ключи. Храните его приватно, вне Git.
Хеши проверяют целостность, но не удостоверяют автора или принадлежность платы.

## Проверка файлов

meshmemo-builder verify-recovery --package ПУТЬ_К_ЭТОМУ_КАТАЛОГУ

Для проверки не требуется исходный файл: полная копия включена в пакет.
Проверка повторно разбирает разметку и OTA, проверяет приложение и сравнивает
rollback-app.bin побайтно с соответствующим диапазоном flash-backup.bin.

## Перед восстановлением на устройстве

1. Подтвердите, что копия принадлежит именно этой плате. Chip ID ESP32-S3 не
   различает модели плат. Проверьте размер flash, ревизию чипа, eFuse, secure
   boot, encryption и anti-rollback. Подписанные/зашифрованные сценарии этим
   пакетом не поддерживаются; неизвестное состояние не разрешает запись.
2. Сохраните текущее состояние платы и настройки отдельно. Сравните текущую
   таблицу разделов и выбранный OTA-слот с recovery.json. При различии остановитесь.
3. Освободите serial port. Только после проверок можно планировать восстановление
   приложения по адресу application.offset из recovery.json. Не меняйте разметку,
   bootloader или OTA-метаданные для обхода несовпадения.
4. После отдельной операции записи проверьте хеш, загрузку, конфигурацию, каналы
   и совместимость старой прошивки с используемой версией MeshMemo.

rollback-app.bin восстанавливает код, но не откатывает миграции настроек новой
прошивкой. Полная копия сохраняется как исходный материал для отдельного ручного
восстановления: её автоматическая запись целиком не предлагается.
В пакете нет команд автопрошивки или файлов с паролем доступа к серверу.
"""


def json_bytes(value: dict) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def payloads(raw: bytes) -> dict[str, bytes]:
    inspection = inspect_flash_bytes(raw)
    if inspection["blockers"]:
        raise BuilderError("Recovery preparation blocked: " + "; ".join(inspection["blockers"]))
    partition = inspection["selected_application"]
    installed = inspection["installed_image"]
    offset, size = partition["offset"], installed["size_bytes"]
    app = raw[offset:offset + size]
    image_info(app, exact=True)
    erase_size = (size + SECTOR - 1) // SECTOR * SECTOR
    if erase_size > partition["size"]:
        raise BuilderError("Recovery application erase range exceeds its partition")
    files = {"flash-backup.bin": raw, "rollback-app.bin": app, "README.md": INSTRUCTIONS.encode("utf-8")}
    manifest = {"schema_version": 1, "kind": "recovery-package", "status": "prepared",
                "device_accessed": False, "flash_authorized": False, "board_identity_verified": False,
                "contains_private_data": True, "inspection": inspection,
                "application": {"offset": offset, "size_bytes": size, "erase_size_bytes": erase_size,
                                "partition_size_bytes": partition["size"], "sha256": installed["sha256"]},
                "files": {name: {"size_bytes": len(data), "sha256": digest(data)} for name, data in files.items()},
                "restore_scope": "application-only after independent live checks",
                "configuration_rollback": "not performed; full backup retained separately in package"}
    files["recovery.json"] = json_bytes(manifest)
    files["SHA256SUMS"] = "".join(f"{digest(files[name])}  {name}\n" for name in sorted(files)).encode("ascii")
    return files


def prepare_recovery(backup: Path, destination: Path) -> Path:
    # Read once: validation, extraction and copied backup refer to exactly these bytes.
    files = payloads(read_binary(backup))
    # Exclusive mkdir also rejects existing empty directories and dangling symlinks.
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    marker = destination / ".incomplete"
    marker.write_bytes(b"Preparation incomplete; do not use this directory.\n")
    # Leave interrupted packages identifiable; never recursively delete caller data.
    for name, data in files.items():
        with (destination / name).open("xb") as output:
            output.write(data)
    verify_recovery(destination, _preparing=True)
    marker.unlink()
    return destination / "recovery.json"


def verify_recovery(package: Path, *, _preparing=False) -> dict:
    if not package.is_dir() or package.is_symlink():
        raise BuilderError("Expected a recovery package directory, not a link")
    expected_names = FILES | ({".incomplete"} if _preparing else set())
    if {path.name for path in package.iterdir()} != expected_names:
        raise BuilderError("Incomplete recovery package or unexpected files")
    for name in expected_names:
        path = package / name
        if path.is_symlink() or not path.is_file():
            raise BuilderError(f"Recovery package requires regular files: {name}")
    raw = read_binary(package / "flash-backup.bin")
    expected = payloads(raw)
    # Derive addresses and hashes from the actual backup, never trust manifest paths.
    # Canonical byte comparison also detects rehashed metadata or instruction edits.
    for name, data in expected.items():
        if name == "flash-backup.bin":
            continue
        actual = read_binary(package / name, max(len(data), 1))
        if actual != data:
            raise BuilderError(f"Recovery package mismatch: {name}")
    manifest = json.loads(expected["recovery.json"])
    return {"schema_version": 1, "kind": "recovery-verification", "status": "verified",
            "device_accessed": False, "flash_authorized": False, "board_identity_verified": False,
            "backup_sha256": digest(raw), "application": manifest["application"],
            "files_verified": len(FILES), "contains_private_data": True}
