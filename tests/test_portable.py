import itertools

import pytest

from meshmemo_builder.environment import load_lock
from meshmemo_builder.pipeline import BuilderError, load_catalog, resolve_plan


def test_identity_distinguishes_boards_and_all_optional_combinations():
    identities = set()
    for board, hardware in load_catalog()["boards"].items():
        available = hardware.get("supported_options", load_catalog()["options"])
        for bits in itertools.product((False, True), repeat=len(available)):
            options = [name for name, enabled in zip(available, bits) if enabled]
            plan = resolve_plan(board=board, options=options)
            assert plan["build_id"] == resolve_plan(board=board, options=reversed(options))["build_id"]
            identities.add(plan["build_id"])
    assert len(identities) == 34


def test_heltec_has_its_own_model_transport_and_dependency_lock():
    plan = resolve_plan(board="heltec-v3")
    assert plan["hardware"]["hardware_model"] == 43
    assert plan["hardware"]["transport"] == "usb-uart-console"
    assert plan["options"] == []
    assert "MESHMEMO_LOCAL_SERIAL=2" in plan["build_flags"]
    lock = load_lock(plan)
    assert lock["board"] == "heltec-v3"
    assert len(lock["packages"]) == 77
    assert "SensorLib" not in {item["name"] for item in lock["packages"]}


@pytest.mark.parametrize("board,model,transport,packages", [
    ("heltec-wsl-v3", 44, "usb-uart-console", 77),
    ("heltec-wireless-tracker", 48, "native-usb-cdc", 78),
    ("tlora-t3s3-v1", 16, "native-usb-cdc", 77),
])
def test_new_board_contract_and_locked_dependencies(board, model, transport, packages):
    plan = resolve_plan(board=board)
    assert plan["hardware"]["hardware_model"] == model
    assert plan["hardware"]["transport"] == transport
    assert plan["options"] == []
    assert plan["hardware"]["status"]["usb"] == "not-tested-with-builder"
    assert plan["hardware"]["status"]["radio"] == "not-tested-with-builder"
    lock = load_lock(plan)
    assert lock["board"] == board and len(lock["packages"]) == packages
    graphics = [p for p in lock["packages"] if p["name"] == "LovyanGFX"]
    assert bool(graphics) == (board == "heltec-wireless-tracker")
    if graphics:
        assert graphics[0]["version"] == "1.2.21"


@pytest.mark.parametrize("option", ["cyrillic", "display-timeout"])
def test_screenless_profile_rejects_display_options(option):
    with pytest.raises(BuilderError, match="does not support options"):
        resolve_plan(board="heltec-wsl-v3", options=[option])
