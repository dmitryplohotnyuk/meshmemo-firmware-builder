import json
import struct

import pytest

from meshmemo_builder.environment import artifacts, load_lock
from meshmemo_builder.pipeline import BuilderError, apply_plan, resolve_plan
from meshmemo_builder.uf2 import application_info
from meshmemo_builder.update import candidate
from test_pipeline import materialize, pinned_files, source_repositories
from test_wizard import session


def test_profiles_use_same_board_but_only_meshmemo_has_adapters():
    full = resolve_plan(board="faketec-v4")
    plain = resolve_plan(board="faketec-v4", profile="patches-only", options=["ua22", "cyrillic"])
    assert full["hardware"]["hardware_model"] == 63
    assert full["hardware"]["environment"] == "nrf52_promicro_diy_tcxo"
    assert not full["windows_workaround"] and not plain["windows_workaround"]
    assert "MESHMEMO_LOCAL_SERIAL=1" in full["build_flags"]
    assert {p["path"] for p in full["copies"] if p["destination"].endswith(("Transport.h", "Sha256.h"))} == {
        "payloads/nrf52840/UsbSfTransport.h", "payloads/nrf52840/UsbSfSha256.h"}
    assert not plain["copies"]
    assert plain["build_flags"] == ["OLED_UA=1"]


def test_windows_workaround_rejected_for_nrf():
    with pytest.raises(BuilderError, match="ESP32-S3"):
        resolve_plan(board="faketec-v4", windows_workaround=True)


def test_flags_only_enter_selected_environment(tmp_path, pinned_files):
    plan = resolve_plan(board="faketec-v4", options=["cyrillic"])
    firmware = materialize(tmp_path, pinned_files)
    ini = firmware / plan["hardware"]["config"]
    before = ini.read_text().split("[env:nrf52_promicro_diy-inkhud]")[1]
    apply_plan(plan, firmware)
    after = ini.read_text()
    assert after.count("-D OLED_UA=1") == 1
    assert after.split("[env:nrf52_promicro_diy-inkhud]")[1] == before


def test_nrf_lock_covers_environment_framework_and_submodules():
    lock = load_lock(resolve_plan(board="faketec-v4"))
    assert lock["environment"] == "nrf52_promicro_diy_tcxo"
    framework = next(p for p in lock["packages"] if p["name"] == "framework-arduinoadafruitnrf52")
    assert len(framework["source_commit"]) == 40
    assert len(framework["submodules"]) == 2
    assert all(item["archive"]["url"].endswith(item["commit"] + ".zip") for item in framework["submodules"])
    downloaded = {relative for _, relative in artifacts(lock)}
    assert all("archives/" + item["archive"]["sha256"] + ".zip" in downloaded for item in framework["submodules"])


REGION = {"offset": 0x26000, "size": 815104, "uf2_family": 0xADA52840}


def uf2_block(address=0x26000, number=0, total=1, family=0xADA52840, flags=0x2000):
    return struct.pack("<8I", 0x0A324655, 0x9E5D5157, flags, address, 256, number, total, family) + bytes(476) + struct.pack("<I", 0x0AB16F30)


def test_uf2_budget_counts_flash_payload_not_container_bytes():
    raw = uf2_block(total=2) + uf2_block(address=0x26200, number=1, total=2)
    info = application_info(raw, REGION)
    assert info["span_bytes"] == 768 and info["payload_bytes"] == 512 and len(raw) == 1024


@pytest.mark.parametrize("raw", [b"", b"bad", uf2_block()[:-1],
    uf2_block(address=0), uf2_block(address=0xED000), uf2_block(address=0x26100),
    uf2_block(family=0), uf2_block(flags=0), uf2_block(number=1), uf2_block(total=2),
    uf2_block(total=2) + uf2_block(number=1, total=2),
    b"bad!" + uf2_block()[4:]])
def test_uf2_rejects_malformed_or_non_application_blocks(raw):
    with pytest.raises(BuilderError):
        application_info(raw, REGION)


def test_nrf_update_planner_fails_with_explicit_architecture_message(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"plan": resolve_plan(board="faketec-v4")}))
    with pytest.raises(BuilderError, match="ESP32-S3 only"):
        candidate(path, "faketec-v4")


def test_wizard_can_prepare_faketec_plain_and_full(tmp_path):
    for prefix in (["1"], ["7", "1"]):
        code, output, calls = session(prefix + ["6", "да", "да", "", "1", str(tmp_path / "faketec"), "1", "да"])
        assert code == 0 and "Faketec V4" in output
        assert calls[0][calls[0].index("--board") + 1] == "faketec-v4"
        assert "--ua22" in calls[0] and "--cyrillic" in calls[0]
