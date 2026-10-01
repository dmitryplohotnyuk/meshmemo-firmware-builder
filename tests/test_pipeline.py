import itertools
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from meshmemo_builder.cli import main
from meshmemo_builder.pipeline import (
    BuilderError, apply_plan, contained, data_root, git, load_catalog, prepare,
    resolve_plan, sha256,
)


def test_default_has_no_personal_options():
    plan = resolve_plan(windows_workaround=False)
    assert plan["options"] == []
    assert plan["build_flags"] == ["MESHTASTIC_USB_SF_BRIDGE=1", "MESHMEMO_LOCAL_SERIAL=1"]
    assert not any("optional/" in patch["path"] for patch in plan["patches"])
    assert not plan["windows_workaround"]


@pytest.mark.parametrize("option,filename,flag", [
    ("ua22", "ua22.patch", None),
    ("cyrillic", "cyrillic.patch", "OLED_UA=1"),
    ("display-timeout", "display-timeout.patch", None),
])
def test_independent_options(option, filename, flag):
    plan = resolve_plan(options=[option], windows_workaround=False)
    optional = [p["path"] for p in plan["patches"] if "optional/" in p["path"]]
    assert optional == ["patches/optional/" + filename]
    assert ("OLED_UA=1" in plan["build_flags"]) == (flag is not None)


@pytest.mark.parametrize("kwargs", [
    {"board": "unknown"}, {"upstream": "develop"}, {"profile": "unknown"},
    {"options": ["unknown"]},
])
def test_unknown_inputs_are_rejected(kwargs):
    with pytest.raises(BuilderError):
        resolve_plan(**kwargs)


@pytest.mark.parametrize("path", ["../escape", "/absolute", "C:/escape", "a\\b", ""])
def test_unsafe_paths_rejected(tmp_path, path):
    with pytest.raises(BuilderError):
        contained(tmp_path, path)


def test_corrupt_patch_rejected_before_destination_is_created(tmp_path):
    root = tmp_path / "assets"
    for directory in ("registry", "patches", "payloads"):
        shutil.copytree(data_root() / directory, root / directory)
    plan = resolve_plan(root=root)
    (root / plan["patches"][0]["path"]).write_bytes(b"broken patch")
    destination = tmp_path / "output"
    with pytest.raises(BuilderError, match="modified asset"):
        prepare(plan, destination, root=root)
    assert not destination.exists()


def test_existing_destination_is_preserved(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    sentinel = existing / "user.txt"
    sentinel.write_text("keep")
    with pytest.raises(BuilderError, match="already exists"):
        prepare(resolve_plan(), existing)
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (existing / "prepare.json").exists()


def test_failed_preparation_never_looks_ready(tmp_path):
    destination = tmp_path / "failed"
    with pytest.raises(BuilderError, match="Source must"):
        prepare(resolve_plan(), destination, firmware_source="--upload-pack=bad")
    assert json.loads((destination / "prepare.json").read_text(encoding="utf-8"))["status"] == "failed"
    with pytest.raises(BuilderError, match="already exists"):
        prepare(resolve_plan(), destination)


def test_cli_option_selection_and_errors(capsys):
    assert main(["inspect", "--cyrillic", "--windows-workaround", "off"]) == 0
    assert json.loads(capsys.readouterr().out)["options"] == ["cyrillic"]
    assert main(["inspect", "--board", "unknown"]) == 1
    assert "Unknown" in capsys.readouterr().err


@pytest.fixture(scope="session")
def source_repositories():
    firmware = os.environ.get("MESHMEMO_TEST_FIRMWARE")
    protobufs = os.environ.get("MESHMEMO_TEST_PROTOBUFS")
    if not firmware or not protobufs:
        pytest.skip("Set MESHMEMO_TEST_FIRMWARE and MESHMEMO_TEST_PROTOBUFS for pinned upstream integration tests")
    return Path(firmware), Path(protobufs)


@pytest.fixture(scope="session")
def pinned_files(source_repositories):
    plan = resolve_plan(options=["ua22", "cyrillic", "display-timeout"], windows_workaround=True)
    result = {}
    created = set()
    for item in plan["patches"]:
        lines = (data_root() / item["path"]).read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if line == "--- /dev/null" and lines[index + 1].startswith("+++ b/"):
                created.add((item["target"], lines[index + 1][6:]))
            if line.startswith("--- a/"):
                relative = line[6:]
                if (item["target"], relative) in created:
                    continue
                repository = source_repositories[item["target"] == "protobufs"]
                commit = plan[item["target"]]["commit"]
                result[(item["target"], relative)] = git(repository, "show", f"{commit}:{relative}")
    for board in load_catalog()["boards"].values():
        relative = board["config"]
        result[("firmware", relative)] = git(source_repositories[0], "show", f"{plan['firmware']['commit']}:{relative}")
    return result


def materialize(tmp_path, files):
    firmware = tmp_path / "firmware"
    firmware.mkdir()
    git(firmware, "init", "-q")
    (firmware / "protobufs").mkdir()
    git(firmware / "protobufs", "init", "-q")
    for (target, relative), content in files.items():
        path = firmware / ("protobufs" if target == "protobufs" else "") / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    return firmware


@pytest.mark.parametrize("enabled", list(itertools.product((False, True), repeat=3)))
@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize("board", ["tbeam-s3-core", "heltec-v3"])
def test_real_upstream_option_matrix(tmp_path, pinned_files, enabled, windows, board):
    options = [key for key, active in zip(("ua22", "cyrillic", "display-timeout"), enabled) if active]
    plan = resolve_plan(board=board, options=options, windows_workaround=windows)
    firmware = materialize(tmp_path, pinned_files)
    apply_plan(plan, firmware)
    radio = (firmware / "src/mesh/RadioInterface.cpp").read_text(encoding="utf-8")
    screen = (firmware / "src/graphics/Screen.h").read_text(encoding="utf-8")
    power = (firmware / "src/PowerFSM.cpp").read_text(encoding="utf-8")
    config = (firmware / plan["hardware"]["config"]).read_text(encoding="utf-8")
    assert ("RDEF(UA_433, 433.0f, 434.7f, 10, 0, 22," in radio) == enabled[0]
    assert ("ch == 0xD2" in screen) == enabled[1]
    assert ("-D OLED_UA=1" in config) == enabled[1]
    assert ("static void serialIdle()" in power) == enabled[2]
    assert "-D MESHTASTIC_USB_SF_BRIDGE=1" in config
    assert (firmware / "windows-build.ini").exists() == windows
    assert "reply[19] = 4;" in (firmware / "src/mesh/UsbSfBridge.cpp").read_text(encoding="utf-8")
    assert f"USB_SF_EXPECTED_MODEL = {plan['hardware']['hardware_model']}" in (firmware / "src/mesh/UsbSfBuild.h").read_text()
    for item in plan["copies"]:
        assert sha256(firmware / item["destination"]) == item["sha256"]


def test_full_local_prepare_pins_commits_and_records_sources(tmp_path, source_repositories):
    plan = resolve_plan(windows_workaround=False)
    destination = tmp_path / "prepared"
    manifest_path = prepare(plan, destination, *map(str, source_repositories))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "prepared"
    assert git(destination / "firmware", "rev-parse", "HEAD").strip() == plan["firmware"]["commit"]
    assert git(destination / "firmware/protobufs", "rev-parse", "HEAD").strip() == plan["protobufs"]["commit"]
    assert git(destination / "firmware", "remote", "get-url", "origin").strip() == plan["firmware"]["url"]
    assert len(manifest["source_sha256"]) > 100
    for path in ("src/mesh/UsbSfRadio.cpp", "src/mesh/UsbSfBridge.cpp", plan["hardware"]["config"]):
        assert manifest["source_sha256"][path] == sha256(destination / "firmware" / path)


def test_all_options_preserve_legacy_sources_except_portable_contract(tmp_path, pinned_files):
    reference = os.environ.get("MESHMEMO_TEST_LEGACY")
    if not reference:
        pytest.skip("Set MESHMEMO_TEST_LEGACY to the existing full-profile checkout")
    plan = resolve_plan(options=["ua22", "cyrillic", "display-timeout"], windows_workaround=True)
    firmware = materialize(tmp_path, pinned_files)
    apply_plan(plan, firmware)
    git(firmware, "apply", "--reverse", data_root() / "patches/core/0007-portable-capabilities.patch")
    paths = {plan["hardware"]["config"]}
    for item in plan["patches"]:
        prefix = "protobufs/" if item["target"] == "protobufs" else ""
        for line in (data_root() / item["path"]).read_text(encoding="utf-8").splitlines():
            if line.startswith("+++ b/"):
                paths.add(prefix + line[6:])
    paths.update(item["destination"] for item in plan["copies"])
    paths -= {"src/mesh/UsbSfTransport.h", "src/mesh/UsbSfSha256.h"}
    for path in sorted(paths):
        actual = (firmware / path).read_text(encoding="utf-8")
        expected = (Path(reference) / path).read_text(encoding="utf-8")
        if path == plan["hardware"]["config"]:
            expected = expected.replace("  -D MESHTASTIC_USB_SF_BRIDGE=1\n", "  -D MESHTASTIC_USB_SF_BRIDGE=1\n  -D MESHMEMO_LOCAL_SERIAL=1\n")
        if path == "windows-build.ini":
            actual = actual.split("\n[env:heltec-v3]")[0]
        if path == "src/mesh/UsbSfRadio.cpp":
            expected = expected.replace('#include "mbedtls/sha256.h"', '#include "UsbSfSha256.h"')
            expected = expected.replace('mbedtls_sha256_ret(material, 4 + chSize + loSize, digest, 0) == 0', 'usbSfSha256(material, 4 + chSize + loSize, digest)')
            expected = expected.replace('mbedtls_sha256_ret(encoded, length, digest, 0) != 0', '!usbSfSha256(encoded, length, digest)')
        assert actual == expected, path
