"""Resolve checked inputs and prepare a fresh, isolated upstream checkout."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from urllib.parse import urlsplit

from . import __version__


class BuilderError(RuntimeError):
    """An actionable input or tool failure."""


def data_root() -> Path:
    packaged = Path(__file__).parent / "data"
    return packaged if packaged.is_dir() else Path(__file__).resolve().parents[2]


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def contained(root: Path, relative: str) -> Path:
    part = PurePosixPath(relative)
    if not relative or part.is_absolute() or ".." in part.parts or "\\" in relative or ":" in relative:
        raise BuilderError(f"Unsafe asset path: {relative}")
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()):
        raise BuilderError(f"Asset escapes its root: {relative}")
    return result


def load_catalog(root: Path | None = None) -> dict:
    root = root or data_root()
    try:
        catalog = json.loads((root / "registry/catalog.json").read_text(encoding="utf-8"))
        if catalog["schema_version"] != 1:
            raise BuilderError("Unsupported registry schema")
        for name in ("boards", "upstreams", "profiles", "options"):
            if not isinstance(catalog[name], dict) or not catalog[name]:
                raise BuilderError(f"Invalid registry section: {name}")
        return catalog
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BuilderError(f"Cannot read registry: {exc}") from exc


def verify_assets(plan: dict, root: Path) -> None:
    for item in plan["patches"] + plan["copies"]:
        if item["target"] not in ("firmware", "protobufs"):
            raise BuilderError("Invalid patch target")
        path = contained(root, item["path"])
        if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise BuilderError(f"Invalid SHA-256: {item['path']}")
        if not path.is_file() or sha256(path) != item["sha256"]:
            raise BuilderError(f"Missing or modified asset: {item['path']}")
        if "destination" in item:
            contained(root, item["destination"])
    item = plan.get("dependency_lock")
    if item:
        path = contained(root, item["path"])
        if not path.is_file() or sha256(path) != item["sha256"]:
            raise BuilderError("Missing or modified dependency lock")


def resolve_plan(board="tbeam-s3-core", upstream="2.7.26", profile="meshmemo",
                 options=(), windows_workaround=None, root: Path | None = None) -> dict:
    root = root or data_root()
    catalog = load_catalog(root)
    try:
        hardware = catalog["boards"][board]
        base = catalog["upstreams"][upstream]
        selected_profile = catalog["profiles"][profile]
    except KeyError as exc:
        raise BuilderError(f"Unknown board, upstream or profile: {exc.args[0]}") from exc
    if upstream not in hardware["upstreams"]:
        raise BuilderError(f"{board} does not support upstream {upstream}")
    if profile not in hardware.get("supported_profiles", catalog["profiles"]):
        raise BuilderError(f"{board} does not support profile {profile}; use --profile patches-only")
    selected = sorted(set(options))
    unknown = set(selected) - catalog["options"].keys()
    if unknown:
        raise BuilderError(f"Unknown options: {', '.join(sorted(unknown))}")
    unsupported = set(selected) - set(hardware.get("supported_options", catalog["options"]))
    if unsupported:
        raise BuilderError(f"{board} does not support options: {', '.join(sorted(unsupported))}")
    for repository in ("firmware", "protobufs"):
        if not re.fullmatch(r"[0-9a-f]{40}", base[repository]["commit"]):
            raise BuilderError(f"Unpinned {repository} commit")
    meshmemo = profile == "meshmemo"
    if not meshmemo:
        hardware = {**hardware, "status": {key: "not-tested-patches-only" for key in ("prepare", "build", "usb", "radio")}}
    windows = (os.name == "nt" and meshmemo and hardware["architecture"] == "esp32-s3") if windows_workaround is None else windows_workaround
    if windows and hardware["architecture"] != "esp32-s3":
        raise BuilderError("Windows LTO workaround applies only to ESP32-S3; use auto or off for this board")
    patches = list(base["patches"]) if meshmemo else []
    flags = []
    for option in selected:
        patches.extend(catalog["options"][option]["patches"])
        flags.extend(catalog["options"][option]["build_flags"])
    if windows:
        patches.extend(catalog["windows_workaround"]["patches"])
        patches.extend(hardware.get("windows_patches", []))
    flags.extend(selected_profile["build_flags"])
    if meshmemo:
        flags.extend(hardware.get("build_flags", []))
    plan = {
        "schema_version": 1, "builder_version": __version__, "board": board,
        "upstream": upstream, "profile": profile, "options": selected,
        "windows_workaround": windows, "hardware": hardware,
        "protocol": selected_profile, "firmware": base["firmware"],
        "protobufs": base["protobufs"], "platformio_version": base["platformio_version"],
        "patches": patches, "copies": [hardware.get("copy_overrides", {}).get(item["destination"], item)
                                        for item in base["copies"]] if meshmemo else [], "build_flags": flags,
        "dependency_lock": catalog["dependency_locks"].get(f"{board}:{upstream}"),
    }
    verify_assets(plan, root)
    plan["build_id"] = plan_identity(plan)
    return plan


def plan_identity(plan: dict) -> str:
    identity = {key: value for key, value in plan.items() if key != "build_id"}
    identity["hardware"] = {key: value for key, value in plan["hardware"].items() if key != "status"}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def resolve_saved_plan(plan: dict) -> dict:
    if "release" in plan:
        from .releases import resolve_release_plan
        return resolve_release_plan(plan["board"], plan["release"], plan["options"],
                                    plan["windows_workaround"], plan["release"]["base"], profile=plan["profile"])
    return resolve_plan(plan["board"], plan["upstream"], plan["profile"],
                        plan["options"], plan["windows_workaround"])


def run(command, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run([str(arg) for arg in command], cwd=cwd, capture_output=True,
                                text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise BuilderError(f"Cannot start {command[0]}: {exc}") from exc
    if result.returncode:
        raise BuilderError(f"{command[0]} failed ({result.returncode}):\n{result.stderr[-4000:]}{result.stdout[-2000:]}")
    return result.stdout


def git(cwd: Path, *args) -> str:
    return run(["git", "-c", f"safe.directory={cwd.resolve().as_posix()}", *args], cwd=cwd)


def clone(source: str, destination: Path, commit: str) -> None:
    local = Path(source)
    if local.is_dir():
        source = str(local.resolve())
        settings = ["-c", f"safe.directory={local.resolve().as_posix()}"]
    else:
        url = urlsplit(source)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise BuilderError("Source must be an existing local Git repository or a credential-free HTTPS URL")
        settings = []
    run(["git", *settings, "clone", "--no-hardlinks", "--no-checkout", "--", source, destination])
    git(destination, "checkout", "--detach", commit)
    if git(destination, "rev-parse", "HEAD").strip() != commit:
        raise BuilderError("Checkout does not match pinned commit")


def apply_plan(plan: dict, firmware: Path, root: Path | None = None, patch_progress=None) -> None:
    root = root or data_root()
    verify_assets(plan, root)
    for item in plan["patches"]:
        target = firmware / "protobufs" if item["target"] == "protobufs" else firmware
        patch = contained(root, item["path"])
        try:
            git(target, "apply", "--check", patch)
            git(target, "apply", patch)
        except BuilderError as exc:
            if patch_progress:
                patch_progress(item, "conflict", str(exc))
            raise BuilderError(f"Patch failed: {item['path']}\n{exc}") from exc
        if patch_progress:
            patch_progress(item, "applied")
    for item in plan["copies"]:
        target = firmware / "protobufs" if item["target"] == "protobufs" else firmware
        destination = contained(target, item["destination"])
        if destination.exists():
            raise BuilderError(f"Refusing to replace existing source: {item['destination']}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(contained(root, item["path"]), destination)
    config = contained(firmware, plan["hardware"]["config"])
    content = config.read_text(encoding="utf-8")
    anchor = plan["hardware"]["flags_anchor"]
    lines = content.splitlines()
    start, end = 0, len(lines)
    if section := plan["hardware"].get("config_section"):
        if lines.count(section) != 1:
            raise BuilderError("Board configuration section does not match")
        start = lines.index(section) + 1
        end = next((i for i in range(start, len(lines)) if lines[i].startswith("[")), len(lines))
    if lines[start:end].count(anchor) != 1:
        raise BuilderError("Board build flag anchor does not match the pinned configuration")
    if not plan["build_flags"]:
        return
    if not all(re.fullmatch(r"[A-Z_0-9]+=[A-Za-z_0-9]+", flag) for flag in plan["build_flags"]):
        raise BuilderError("Invalid build flag in registry")
    at = lines.index(anchor, start, end) + 1
    lines[at:at] = [f"  -D {flag}" for flag in plan["build_flags"]]
    config.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    if plan["profile"] != "meshmemo":
        return
    build_header = firmware / "src/mesh/UsbSfBuild.h"
    if build_header.exists():
        raise BuilderError("Refusing to overwrite an existing build identity header")
    identity = ", ".join(f"0x{plan['build_id'][index:index + 2]}" for index in range(0, 64, 2))
    build_header.write_text(
        "#pragma once\n#include <cstdint>\n"
        f"static constexpr uint32_t USB_SF_EXPECTED_MODEL = {plan['hardware']['hardware_model']};\n"
        f"static constexpr uint8_t USB_SF_BUILD_ID[32] = {{{identity}}};\n",
        encoding="utf-8", newline="\n")
    if "release" in plan:
        bridge = firmware / "src/mesh/UsbSfBridge.cpp"
        content = bridge.read_text(encoding="utf-8")
        baseline = load_catalog(root)["upstreams"][plan["release"]["base"]]["firmware"]["commit"]
        anchor = f'memcpy(reply + 28, "{baseline}", 40);'
        if content.count(anchor) != 1:
            raise BuilderError("Release HELLO firmware identity anchor does not match")
        content = content.replace(anchor, f'memcpy(reply + 28, "{plan["firmware"]["commit"]}", 40);')
        bridge.write_text(content, encoding="utf-8", newline="\n")


def source_hashes(firmware: Path, plan: dict) -> dict[str, str]:
    paths = set()
    for directory, prefix in ((firmware, ""), (firmware / "protobufs", "protobufs/")):
        for path in git(directory, "ls-files", "-z").split("\0"):
            if path and (directory / path).is_file():
                paths.add(prefix + path)
        for path in git(directory, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
            if path and (directory / path).is_file():
                paths.add(prefix + path)
    return {path: sha256(contained(firmware, path)) for path in sorted(paths)}


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def prepare(plan: dict, destination: Path, firmware_source: str | None = None,
            protobuf_source: str | None = None, root: Path | None = None) -> Path:
    root = root or data_root()
    verify_assets(plan, root)
    destination = destination.resolve()
    try:
        destination.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise BuilderError(f"Destination already exists; choose a new directory: {destination}") from exc
    manifest = {"status": "preparing", "plan": plan}
    path = destination / "prepare.json"
    write_json(path, manifest)
    try:
        firmware = destination / "firmware"
        clone(firmware_source or plan["firmware"]["url"], firmware, plan["firmware"]["commit"])
        clone(protobuf_source or plan["protobufs"]["url"], firmware / "protobufs", plan["protobufs"]["commit"])
        # Upstream's build metadata parser expects an HTTPS origin, not a local Windows path.
        git(firmware, "remote", "set-url", "origin", plan["firmware"]["url"])
        git(firmware / "protobufs", "remote", "set-url", "origin", plan["protobufs"]["url"])
        return finish_prepare(plan, destination, root)
    except (BuilderError, OSError) as exc:
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        write_json(path, manifest)
        raise


def finish_prepare(plan, destination, root=None, patch_progress=None):
    """Apply and record a freshly cloned checkout, shared by both source flows."""
    firmware = destination / "firmware"
    path = destination / "prepare.json"
    manifest = {"status": "preparing", "plan": plan}
    write_json(path, manifest)
    try:
        if "release" in plan:
            from .releases import validate_sources
            validate_sources(plan, firmware)
        apply_plan(plan, firmware, root, patch_progress)
        manifest["source_sha256"] = source_hashes(firmware, plan)
        manifest["status"] = "prepared"
        write_json(path, manifest)
    except (BuilderError, OSError) as exc:
        manifest.update(status="failed", error=str(exc))
        write_json(path, manifest)
        raise
    return path
