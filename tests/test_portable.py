import itertools

from meshmemo_builder.environment import load_lock
from meshmemo_builder.pipeline import resolve_plan


def test_identity_distinguishes_boards_and_all_optional_combinations():
    identities = set()
    for board in ("tbeam-s3-core", "heltec-v3"):
        for bits in itertools.product((False, True), repeat=3):
            options = [name for name, enabled in zip(("ua22", "cyrillic", "display-timeout"), bits) if enabled]
            plan = resolve_plan(board=board, options=options)
            assert plan["build_id"] == resolve_plan(board=board, options=reversed(options))["build_id"]
            identities.add(plan["build_id"])
    assert len(identities) == 16


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
