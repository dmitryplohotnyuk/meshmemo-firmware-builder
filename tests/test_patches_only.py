import hashlib
import json

import pytest

from meshmemo_builder.cli import main
from meshmemo_builder.pipeline import BuilderError, apply_plan, load_catalog, resolve_plan, resolve_saved_plan
from meshmemo_builder.releases import baseline_inputs, resolve_release_plan, tree_digest
from meshmemo_builder.update import candidate
from test_pipeline import materialize, pinned_files, source_repositories
from test_update import application
from test_wizard import session


CASES = [(board, options) for board, hardware in load_catalog()["boards"].items()
         for options in ([], ["ua22"], ["cyrillic"], ["ua22", "cyrillic"], ["display-timeout"])
         if set(options) <= set(hardware.get("supported_options", load_catalog()["options"]))]


@pytest.mark.parametrize("board,options", CASES)
def test_only_requested_source_files_change(tmp_path, pinned_files, board, options):
    plan = resolve_plan(board=board, profile="patches-only", options=options)
    assert not plan["windows_workaround"]
    assert plan["copies"] == []
    assert plan["build_flags"] == (["OLED_UA=1"] if "cyrillic" in options else [])
    assert len(plan["patches"]) == len(options)
    assert all(p["path"].startswith("patches/optional/") for p in plan["patches"])
    firmware = materialize(tmp_path, pinned_files)
    def snapshot():
        return {p.relative_to(firmware).as_posix(): p.read_bytes() for p in firmware.rglob("*")
                if p.is_file() and ".git" not in p.parts}
    before = snapshot()
    apply_plan(plan, firmware)
    after = snapshot()
    expected = set()
    if "ua22" in options:
        expected.add("src/mesh/RadioInterface.cpp")
    if "cyrillic" in options:
        expected.update(["src/graphics/Screen.h", plan["hardware"]["config"]])
    if "display-timeout" in options:
        expected.add("src/PowerFSM.cpp")
    assert {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)} == expected
    assert not (firmware / "src/mesh/UsbSfBuild.h").exists()
    assert not (firmware / "src/mesh/UsbSfBridge.cpp").exists()
    assert not (firmware / "src/mesh/UsbSfRadio.cpp").exists()
    assert resolve_saved_plan(plan) == plan


def test_explicit_windows_workaround_is_independent_of_meshmemo():
    plan = resolve_plan(profile="patches-only", options=["ua22"], windows_workaround=True)
    assert [p["path"] for p in plan["patches"]] == ["patches/optional/ua22.patch", "patches/platform/windows-build.patch"]
    assert not plan["copies"] and not plan["build_flags"]


def test_release_reconstruction_preserves_patches_only_profile():
    pinned = {"tag": "v9.0.0.fixture", "firmware_commit": "a" * 40, "protobuf_commit": "b" * 40,
              "base": "2.7.26", "build_inputs_sha256": tree_digest(baseline_inputs("2.7.26"))}
    plan = resolve_release_plan("tbeam-s3-core", pinned, options=["cyrillic"], profile="patches-only")
    assert resolve_saved_plan(plan) == plan
    assert plan["profile"] == "patches-only" and not plan["windows_workaround"]
    assert [p["path"] for p in plan["patches"]] == ["patches/optional/cyrillic.patch"]


def test_cli_inspection_has_no_meshmemo_payloads(capsys):
    assert main(["inspect", "--profile", "patches-only", "--ua22"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["options"] == ["ua22"] and not plan["copies"]
    assert not plan["build_flags"] and len(plan["patches"]) == 1


def test_update_planner_does_not_treat_plain_image_as_meshmemo(tmp_path):
    plan = resolve_plan(profile="patches-only", options=["ua22"])
    raw = application()
    (tmp_path / "application.bin").write_bytes(raw)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"status": "built", "artifact_kind": "application-only", "plan": plan,
        "artifact": "application.bin", "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}))
    with pytest.raises(BuilderError, match="patches-only has no bridge"):
        candidate(manifest, "tbeam-s3-core")


def test_wizard_optional_only_pinned_and_release(tmp_path):
    code, output, calls = session(["7", "1", "1", "да", "", "", "1", str(tmp_path / "ua"), "1", "да"])
    assert code == 0 and len(calls) == 1
    assert calls[0][calls[0].index("--profile") + 1] == "patches-only"
    assert "--ua22" in calls[0] and "--cyrillic" not in calls[0]
    assert "без MeshMemo" in output and "USF2" not in output
    code, _, calls = session(["7", "2", "1", "2", "", "", "да", "", str(tmp_path / "cyr"), "да"])
    assert code == 0 and calls[0][0] == "prepare-release"
    assert calls[0][calls[0].index("--profile") + 1] == "patches-only"
    assert "--cyrillic" in calls[0] and "--ua22" not in calls[0]
