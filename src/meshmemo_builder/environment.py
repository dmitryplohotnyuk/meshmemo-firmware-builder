"""Download checksum-locked inputs, then provision a separate offline runtime."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import platform
import shutil
import subprocess
import sys
from urllib.request import Request, urlopen

from .pipeline import BuilderError, contained, data_root, load_catalog, sha256, write_json


def load_lock(plan):
    item = plan.get("dependency_lock")
    if not item:
        raise BuilderError("No compatible dependency lock for this release; a maintainer must resolve and validate its new dependencies")
    path = contained(data_root(), item["path"])
    if sha256(path) != item["sha256"]:
        raise BuilderError("Dependency lock checksum mismatch")
    lock = json.loads(path.read_text(encoding="utf-8"))
    firmware_commit = plan["firmware"]["commit"]
    if "release" in plan:
        from .pipeline import resolve_saved_plan
        expected = resolve_saved_plan(plan)
        if expected["dependency_lock"] != item:
            raise BuilderError("Release inputs cannot reuse this dependency lock")
        firmware_commit = load_catalog()["upstreams"][plan["release"]["base"]]["firmware"]["commit"]
    if (lock["schema_version"] != 1 or lock["board"] != plan["board"]
            or lock["firmware_commit"] != firmware_commit
            or lock["platformio_version"] != plan["platformio_version"]):
        raise BuilderError("Dependency lock does not match the prepared firmware")
    return lock


def artifacts(lock):
    for item in lock["packages"]:
        archive = item["archive"]
        yield archive, "archives/" + archive["sha256"] + archive["suffix"]
    for item in lock["python"]:
        yield item, "wheels/" + item["filename"]


def check_artifact(path, item):
    if not path.is_file() or path.stat().st_size != item["size"] or sha256(path) != item["sha256"]:
        raise BuilderError(f"Missing or modified dependency: {path}")


def fetch(lock, cache):
    cache = cache.resolve()
    def download(pair):
        item, relative = pair
        path = contained(cache, relative)
        if path.exists():
            check_artifact(path, item)
            return
        if not item["url"].startswith("https://"):
            raise BuilderError("Dependency downloads require HTTPS")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".partial")
        print(f"Downloading {path.name}", flush=True)
        try:
            with urlopen(Request(item["url"], headers={"User-Agent": "MeshMemo-Builder"}), timeout=120) as response:
                if not response.url.startswith("https://"):
                    raise BuilderError("Dependency redirect must use HTTPS")
                with temporary.open("wb") as output:
                    shutil.copyfileobj(response, output)
            check_artifact(temporary, item)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(download, artifacts(lock)))
    return cache


def check_host(lock):
    if (sys.platform != "win32" or platform.machine().upper() != "AMD64"
            or lock["system"] != "windows_amd64"
            or platform.python_version() != lock["python_version"]):
        raise BuilderError(f"This lock requires Windows AMD64 and Python {lock['python_version']}")


def offline_environment():
    env = os.environ.copy()
    # Do not inherit a user's package index or Python import overrides.
    for key in list(env):
        if key.startswith(("PIP_", "PYTHON", "PLATFORMIO_")):
            del env[key]
    env.update(PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_CONFIG_FILE=os.devnull,
               PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
               PYTHONNOUSERSITE="1", PYTHONPATH=str(data_root() / "payloads/runtime"),
               MESHMEMO_OFFLINE_BUILD="1", PLATFORMIO_SETTING_ENABLE_TELEMETRY="No",
               PLATFORMIO_SETTING_CHECK_PLATFORMIO_INTERVAL="0", PLATFORMIO_DISABLE_UPGRADE_CHECK="true")
    return env


def tree_hashes(runtime):
    result = {}
    def fingerprint(path):
        relative = path.relative_to(runtime)
        # PlatformIO rewrites this set of requested libraries in arbitrary order.
        # Package files themselves remain checked, including nested integrity.dat.
        if len(relative.parts) == 3 and relative.parts[0] == "libdeps" and relative.name == "integrity.dat":
            return None
        if "__pycache__" not in path.parts and path.suffix != ".pyc" and path.is_file():
            return relative.as_posix(), sha256(path)
        return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        for root in ("core/platforms", "core/packages", "core/lib", "libdeps", "python/Lib/site-packages", "python/Scripts"):
            for item in pool.map(fingerprint, (runtime / root).rglob("*")):
                if item:
                    result[item[0]] = item[1]
    return dict(sorted(result.items()))


def bootstrap(plan, cache, destination):
    lock = load_lock(plan)
    check_host(lock)
    cache, destination = cache.resolve(), destination.resolve()
    if destination.exists():
        raise BuilderError("Runtime destination already exists; choose a new directory")
    for item, relative in artifacts(lock):
        check_artifact(contained(cache, relative), item)
    destination.mkdir(parents=True)
    receipt = {"status": "installing", "dependency_lock": plan["dependency_lock"],
               "system": lock["system"], "python_version": lock["python_version"]}
    receipt_path = destination / "runtime.json"
    write_json(receipt_path, receipt)
    env = offline_environment()
    env["PLATFORMIO_CORE_DIR"] = str(destination / "core")
    python = destination / "python/Scripts/python.exe"
    log_path = destination / "install.log"
    def execute(command, log):
        process = subprocess.run([str(x) for x in command], env=env, stdout=log, stderr=subprocess.STDOUT)
        if process.returncode:
            raise BuilderError(f"Offline runtime installation failed; inspect {log_path}")
    try:
        with log_path.open("w", encoding="utf-8") as log:
            execute([sys.executable, "-m", "venv", destination / "python"], log)
            requirements = destination / "requirements.lock"
            requirements.write_text("\n".join(f"{p['name']}=={p['version']} --hash=sha256:{p['sha256']}"
                                               for p in lock["python"]) + "\n", encoding="utf-8")
            execute([python, "-m", "pip", "install", "--no-index", "--no-deps", "--no-cache-dir",
                     "--only-binary=:all:", "--require-hashes", "--find-links", cache / "wheels",
                     "-r", requirements], log)
            execute([python, "-m", "pip", "check"], log)
            execute([python, data_root() / "payloads/runtime/install_locked.py",
                     contained(data_root(), plan["dependency_lock"]["path"]), cache, destination], log)
        print("Recording installed dependency checksums...", flush=True)
        receipt["files"] = tree_hashes(destination)
        receipt["status"] = "ready"
        write_json(receipt_path, receipt)
    except (BuilderError, OSError) as exc:
        receipt.update(status="failed", error=str(exc))
        write_json(receipt_path, receipt)
        raise
    return receipt_path


def verify_runtime(plan, runtime):
    lock = load_lock(plan)
    check_host(lock)
    try:
        receipt = json.loads((runtime / "runtime.json").read_text(encoding="utf-8"))
        if receipt["status"] != "ready" or receipt["dependency_lock"] != plan["dependency_lock"]:
            raise BuilderError("Runtime is incomplete or uses another dependency lock")
        if not receipt["files"] or tree_hashes(runtime) != receipt["files"]:
            raise BuilderError("Runtime dependencies changed; bootstrap a new runtime")
    except (OSError, ValueError, KeyError) as exc:
        raise BuilderError(f"Invalid runtime receipt: {exc}") from exc
    return receipt
