import json
import subprocess
import sys

import pytest

from meshmemo_builder import environment as module
from meshmemo_builder.pipeline import BuilderError, resolve_plan, sha256, write_json


def test_bundled_lock_matches_plan_and_covers_tools():
    lock = module.load_lock(resolve_plan())
    assert len(lock["packages"]) == 78
    assert len(lock["python"]) == 36
    assert {"platformio", "grpcio-tools", "cryptography", "pip"} <= {p["name"] for p in lock["python"]}
    assert {"tool-scons", "framework-arduinoespressif32"} <= {p["name"] for p in lock["packages"]}
    assert all(len(item["sha256"]) == 64 and item["size"] > 0 for item, _ in module.artifacts(lock))


def test_changed_lock_is_rejected():
    plan = resolve_plan()
    plan["dependency_lock"] = {**plan["dependency_lock"], "sha256": "0" * 64}
    with pytest.raises(BuilderError, match="checksum"):
        module.load_lock(plan)


@pytest.mark.parametrize("contents", [None, b"bad", b"same-size"])
def test_missing_or_modified_download_is_rejected(tmp_path, contents):
    path = tmp_path / "archive"
    path.write_bytes(b"good-data")
    item = {"size": 9, "sha256": sha256(path)}
    if contents is None:
        path.unlink()
    else:
        path.write_bytes(contents)
    with pytest.raises(BuilderError, match="dependency"):
        module.check_artifact(path, item)


def test_cached_download_is_verified_without_network(tmp_path, monkeypatch):
    path = tmp_path / "wheels/example.whl"
    path.parent.mkdir()
    path.write_bytes(b"wheel")
    lock = {"packages": [], "python": [{"filename": path.name, "size": 5, "sha256": sha256(path)}]}
    monkeypatch.setattr(module, "urlopen", lambda *a, **k: pytest.fail("network access"))
    module.fetch(lock, tmp_path)
    path.write_bytes(b"wrong")
    with pytest.raises(BuilderError):
        module.fetch(lock, tmp_path)


def test_wrong_host_is_rejected(monkeypatch):
    monkeypatch.setattr(module.platform, "python_version", lambda: "3.11.0")
    with pytest.raises(BuilderError, match="Python 3.12.14"):
        module.check_host(module.load_lock(resolve_plan()))


@pytest.mark.parametrize("change", ["modify", "add", "remove", "failed", "global_library"])
def test_runtime_drift_is_rejected(tmp_path, monkeypatch, change):
    monkeypatch.setattr(module, "check_host", lambda lock: None)
    plan = resolve_plan()
    target = tmp_path / "core/packages/example/source.cpp"
    target.parent.mkdir(parents=True)
    target.write_text("original")
    receipt = {"status": "ready", "dependency_lock": plan["dependency_lock"], "files": module.tree_hashes(tmp_path)}
    write_json(tmp_path / "runtime.json", receipt)
    module.verify_runtime(plan, tmp_path)
    if change == "modify":
        target.write_text("modified")
    elif change == "add":
        (target.parent / "extra.cpp").write_text("extra")
    elif change == "remove":
        target.unlink()
    elif change == "global_library":
        extra = tmp_path / "core/lib/unlocked/source.cpp"
        extra.parent.mkdir(parents=True)
        extra.write_text("unexpected global library")
    else:
        receipt["status"] = "failed"
        write_json(tmp_path / "runtime.json", receipt)
    with pytest.raises(BuilderError):
        module.verify_runtime(plan, tmp_path)


def test_offline_python_refuses_network():
    result = subprocess.run([sys.executable, "-c", "import socket; socket.getaddrinfo('example.com',443)"],
                            env=module.offline_environment(), capture_output=True, text=True)
    assert result.returncode != 0
    assert "MeshMemo offline build" in result.stderr


def test_missing_cache_does_not_create_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "check_host", lambda lock: None)
    destination = tmp_path / "runtime"
    with pytest.raises(BuilderError, match="dependency"):
        module.bootstrap(resolve_plan(), tmp_path / "empty", destination)
    assert not destination.exists()


def test_failed_install_receipt_cannot_look_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "check_host", lambda lock: None)
    monkeypatch.setattr(module, "artifacts", lambda lock: [])
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1))
    destination = tmp_path / "runtime"
    with pytest.raises(BuilderError, match="installation failed"):
        module.bootstrap(resolve_plan(), tmp_path / "cache", destination)
    receipt = json.loads((destination / "runtime.json").read_text())
    assert receipt["status"] == "failed"
    assert "files" not in receipt


def test_poisoned_download_is_not_committed(tmp_path, monkeypatch):
    import io
    class Response(io.BytesIO):
        url = "https://example.invalid/archive"
    monkeypatch.setattr(module, "urlopen", lambda *a, **k: Response(b"bad"))
    lock = {"packages": [], "python": [{"filename": "bad.whl", "size": 3,
            "sha256": "0" * 64, "url": "https://example.invalid/archive"}]}
    with pytest.raises(BuilderError, match="dependency"):
        module.fetch(lock, tmp_path)
    assert not list(tmp_path.rglob("*.whl*"))


def test_only_platformio_integrity_cache_is_excluded(tmp_path):
    storage = tmp_path / "libdeps/board"
    library = storage / "example"
    library.mkdir(parents=True)
    (storage / "integrity.dat").write_text("generated cache")
    source = library / "integrity.dat"
    source.write_text("actual package file")
    hashes = module.tree_hashes(tmp_path)
    assert hashes == {"libdeps/board/example/integrity.dat": sha256(source)}
