"""Compile prepared firmware without opening or flashing a device."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess

from .pipeline import BuilderError, contained, data_root, resolve_saved_plan, run, sha256, source_hashes, write_json
from .environment import offline_environment, verify_runtime


def app_partition(path: Path) -> dict:
    raw = path.read_bytes()
    for offset in range(0, len(raw) - 31, 32):
        magic, kind, subtype, address, size, label, flags = struct.unpack_from("<HBBII16sI", raw, offset)
        if magic == 0x50AA and kind == 0 and subtype in (0, 0x10):
            if size == 0:
                break
            return {"name": label.rstrip(b"\0").decode("ascii"), "offset": address, "size": size}
    raise BuilderError("Generated partition table has no supported application partition")


@contextmanager
def short_paths(firmware: Path, core: Path):
    mappings = []
    try:
        result = []
        for path in (firmware, core):
            if os.name != "nt":
                result.append(path)
                continue
            for letter in "QRSTUVWXYZDEFGHIJKLMNOP":
                drive = f"{letter}:"
                if not Path(drive + "\\").exists():
                    try:
                        run(["subst", drive, path])
                    except BuilderError:
                        continue
                    mappings.append(drive)
                    result.append(Path(drive + "\\"))
                    break
            else:
                raise BuilderError("No free drive letter for short Windows build paths")
        yield result
    finally:
        for drive in reversed(mappings):
            run(["subst", drive, "/D"])


def read_prepared(workspace: Path) -> dict:
    try:
        state = json.loads((workspace / "prepare.json").read_text(encoding="utf-8"))
        if state["status"] != "prepared":
            raise BuilderError("Workspace preparation did not complete")
        plan = state["plan"]
        expected = resolve_saved_plan(plan)
        def inputs(value):
            # Status and the old filename hint do not affect source preparation.
            return {**value, "hardware": {key: item for key, item in value["hardware"].items()
                                         if key not in ("status", "artifact")}}

        if inputs(plan) != inputs(expected):
            raise BuilderError("Prepared plan differs from the installed registry; prepare a new workspace")
        if "release" in plan:
            from .releases import validate_sources
            validate_sources(plan, workspace / "firmware")
        if not state["source_sha256"]:
            raise BuilderError("Prepared workspace has no source hashes")
        for relative, digest in state["source_sha256"].items():
            source = contained(workspace / "firmware", relative)
            if not source.is_file() or sha256(source) != digest:
                raise BuilderError(f"Prepared source changed: {relative}; prepare a new workspace")
        if (workspace / "firmware/.git").exists():
            if source_hashes(workspace / "firmware", plan) != state["source_sha256"]:
                raise BuilderError("Source file set changed; prepare a new workspace")
        return state
    except (KeyError, ValueError, TypeError) as exc:
        raise BuilderError(f"Invalid prepare manifest: {exc}") from exc


def build(workspace: Path, runtime: Path, output: Path, jobs=4) -> Path:
    workspace, runtime, output = (p.resolve() for p in (workspace, runtime, output))
    if jobs < 1:
        raise BuilderError("Jobs must be positive")
    state = read_prepared(workspace)
    plan = state["plan"]
    if (os.name == "nt" and plan["profile"] == "meshmemo" and
            plan["hardware"]["architecture"] == "esp32-s3" and not plan["windows_workaround"]):
        raise BuilderError("Prepare with Windows workaround before compiling on Windows")
    if output.exists():
        raise BuilderError("Output directory already exists; choose a new directory")
    print("Checking locked runtime checksums...", flush=True)
    verify_runtime(plan, runtime)
    python = runtime / "python/Scripts/python.exe"
    firmware = workspace / "firmware"
    log_path = workspace / "build.log"
    report_path = workspace / "build.json"
    report = {"status": "building", "plan": plan}
    write_json(report_path, report)
    try:
        with short_paths(firmware, runtime) as (project_path, runtime_path):
            environment = offline_environment()
            environment["PLATFORMIO_CORE_DIR"] = str(runtime_path / "core")
            environment["PLATFORMIO_LIBDEPS_DIR"] = str(runtime_path / "libdeps")
            command = [str(python), "-m", "platformio"]
            version = subprocess.run(command + ["--version"], env=environment, capture_output=True,
                                     text=True, encoding="utf-8", errors="replace")
            if version.returncode or version.stdout.strip() != f"PlatformIO Core, version {plan['platformio_version']}":
                raise BuilderError(f"Requires PlatformIO {plan['platformio_version']} in {python}")
            print(f"Compiling {plan['board']}. Log: {log_path}", flush=True)
            with log_path.open("w", encoding="utf-8") as log:
                process = subprocess.run(command + ["run", "--project-dir", str(project_path),
                                         "-e", plan["hardware"]["environment"], "-j", str(jobs)],
                                         env=environment, stdout=log, stderr=subprocess.STDOUT)
            if process.returncode:
                raise BuilderError(f"Firmware compilation failed; inspect {log_path}")
            inventory = subprocess.run(command + ["pkg", "list", "--project-dir", str(project_path),
                                       "-e", plan["hardware"]["environment"]], env=environment,
                                       capture_output=True, text=True, encoding="utf-8", errors="replace")
            if inventory.returncode:
                raise BuilderError(f"Could not record resolved build dependencies: {inventory.stderr[-2000:]}")
        verify_runtime(plan, runtime)
        build_dir = firmware / ".pio/build" / plan["hardware"]["environment"]
        nrf = plan["hardware"]["architecture"] == "nrf52840"
        extension = "uf2" if nrf else "bin"
        candidates = [p for p in build_dir.glob(f"firmware-{plan['hardware']['environment']}-*.{extension}")
                      if not p.name.endswith(f".factory.{extension}")]
        if len(candidates) != 1:
            raise BuilderError("Build did not produce exactly one application image")
        image = candidates[0]
        size = image.stat().st_size
        if nrf:
            from .uf2 import application_info
            region = plan["hardware"]["application"]
            info = application_info(image.read_bytes(), region)
            if plan["profile"] == "meshmemo":
                raw = image.read_bytes()
                payload = b"".join(raw[i + 32:i + 288] for i in range(0, len(raw), 512))
                if bytes.fromhex(plan["build_id"]) not in payload:
                    raise BuilderError("UF2 application does not contain the expected MeshMemo build ID")
            partition = {"name": "application", "offset": region["offset"], "size": region["size"]}
            used = info["span_bytes"]
            report["uf2"] = info
        else:
            partition = app_partition(build_dir / "partitions.bin")
            used = size
        if used == 0 or used > partition["size"]:
            raise BuilderError(f"Application size {used} does not fit the application region {partition['size']}")
        output.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(image, output / image.name)
        report.update({
            "status": "built", "artifact": image.name, "artifact_kind": "application-uf2" if nrf else "application-only",
            "meshmemo_enabled": plan["profile"] == "meshmemo",
            "build_id_embedded": plan["profile"] == "meshmemo",
            "sha256": sha256(image), "size_bytes": size, "build_partition": partition,
            "remaining_partition_bytes": partition["size"] - used,
            "platformio_version": plan["platformio_version"],
            "dependency_lock": plan["dependency_lock"], "offline_build": True,
            "runtime_receipt_sha256": sha256(runtime / "runtime.json"),
            "device_partition_check_required": not nrf,
            "hardware_validation": {"usb": "not-run", "radio": "not-run"},
        })
        if nrf:
            report["bootloader_compatibility_check_required"] = True
        (output / "dependencies.txt").write_text(inventory.stdout, encoding="utf-8")
        shutil.copyfile(log_path, output / "build.log")
        shutil.copyfile(workspace / "prepare.json", output / "prepare.json")
        shutil.copyfile(runtime / "runtime.json", output / "runtime.json")
        shutil.copyfile(contained(data_root(), plan["dependency_lock"]["path"]), output / "dependency-lock.json")
        report["dependency_inventory_sha256"] = sha256(output / "dependencies.txt")
        report["build_log_sha256"] = sha256(output / "build.log")
        write_json(output / "manifest.json", report)
        write_json(report_path, report)
    except (BuilderError, OSError) as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        write_json(report_path, report)
        raise
    return output / "manifest.json"
