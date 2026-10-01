"""Maintainer-only: resolve an explicitly selected, previously tested Windows environment."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from importlib import metadata
import json
from pathlib import Path
import platform
import sys
from urllib.parse import quote
from urllib.request import Request, urlopen

from packaging.requirements import Requirement
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename


def fetch_json(url):
    with urlopen(Request(url, headers={"User-Agent": "MeshMemo-lock-resolver/0.1"}), timeout=60) as response:
        return json.load(response)


def resolve_package(record, scratch):
    spec = record["spec"]
    if spec["uri"]:
        url = spec["uri"]
        if not url.startswith("https://"):
            raise ValueError(f"Non-HTTPS package: {record['name']}")
        path = scratch / (hashlib.sha256(url.encode()).hexdigest() + ".zip")
        with urlopen(Request(url, headers={"User-Agent": "MeshMemo-lock-resolver/0.1"}), timeout=120) as response:
            with path.open("wb") as stream:
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
        raw = path.read_bytes()
        record["archive"] = {"url": url, "sha256": hashlib.sha256(raw).hexdigest(),
                             "size": len(raw), "suffix": ".zip"}
    else:
        api = "https://api.registry.platformio.org/v3/packages/{}/{}/{}".format(
            quote(spec["owner"]), record["type"], quote(spec["name"]))
        package = fetch_json(api)
        version = next(v for v in package["versions"] if v["name"] == record["version"])
        candidates = [f for f in version["files"] if f.get("system", "*") in ("*", "windows_amd64")
                      or "windows_amd64" in (f.get("system") if isinstance(f.get("system"), list) else [])]
        if len(candidates) != 1:
            raise ValueError(f"Ambiguous Windows archive for {record['name']}: {candidates}")
        artifact = candidates[0]
        record["archive"] = {"url": artifact["download_url"], "sha256": artifact["checksum"]["sha256"],
                             "size": artifact["size"], "suffix": ".tar.gz" if artifact["name"].endswith(".tar.gz") else ".zip"}
    print(f"Resolved {record['type']}: {record['name']} {record['version']}", flush=True)
    return record


def python_closure(contrib):
    installed = {canonicalize_name(d.metadata["Name"]): d for d in metadata.distributions()}
    installed.update({canonicalize_name(d.metadata["Name"]): d for d in metadata.distributions(path=[str(contrib)])})
    pending = ["platformio", "pip", "setuptools", "grpcio-tools", "PyYAML", "intelhex",
               "cryptography", "ecdsa", "bitstring", "reedsolo"]
    selected = {}
    while pending:
        name = canonicalize_name(pending.pop())
        if name in selected:
            continue
        dist = installed[name]
        selected[name] = dist.version
        for raw in dist.requires or []:
            req = Requirement(raw)
            if req.marker and not req.marker.evaluate({"extra": ""}):
                continue
            dep = installed[canonicalize_name(req.name)]
            if req.specifier and not req.specifier.contains(dep.version):
                raise ValueError(f"Reference Python dependency conflict: {raw}, installed {dep.version}")
            pending.append(req.name)
    compatible = list(sys_tags())
    result = []
    for name, version in sorted(selected.items()):
        data = fetch_json(f"https://pypi.org/pypi/{name}/{version}/json")
        wheels = []
        for item in data["urls"]:
            if not item["filename"].endswith(".whl"):
                continue
            tags = parse_wheel_filename(item["filename"])[3]
            priorities = [i for i, tag in enumerate(compatible) if tag in tags]
            if priorities:
                wheels.append((min(priorities), item))
        item = min(wheels, key=lambda row: row[0])[1]
        result.append({"name": name, "version": version, "filename": item["filename"],
                       "url": item["url"], "sha256": item["digests"]["sha256"], "size": item["size"]})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-core", type=Path, required=True)
    parser.add_argument("--reference-libraries", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "win32" or platform.machine().upper() != "AMD64":
        parser.error("This lock resolver currently targets Windows AMD64 only")
    args.scratch.mkdir(parents=True, exist_ok=False)
    paths = [args.reference_core / "platforms/espressif32/.piopm"]
    paths += [args.reference_core / "packages" / name / ".piopm" for name in (
        "framework-arduinoespressif32", "tool-esptoolpy", "tool-mklittlefs",
        "tool-openocd-esp32", "toolchain-riscv32-esp", "toolchain-xtensa-esp32s3", "tool-scons")]
    paths += sorted(args.reference_libraries.glob("*/.piopm"))
    records = []
    for path in paths:
        item = json.loads(path.read_text(encoding="utf-8"))
        item["directory"] = path.parent.name
        records.append(item)
    with ThreadPoolExecutor(max_workers=4) as pool:
        packages = list(pool.map(lambda item: resolve_package(item, args.scratch), records))
    wheels = python_closure(args.reference_core / "packages/tool-esptoolpy/_contrib")
    result = {"schema_version": 1, "id": "tbeam-s3-core-2.7.26-windows-amd64",
              "system": "windows_amd64", "python_version": platform.python_version(),
              "platformio_version": "6.2.0", "firmware_commit": "54e0d8d0ab2ff56b3a9ce967e53f79e49af560fb",
              "board": "tbeam-s3-core", "packages": packages, "python": wheels}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"Wrote {len(packages)} source/tool archives and {len(wheels)} Python wheels: {args.output}")


if __name__ == "__main__":
    main()
