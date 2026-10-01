"""Install verified archives without resolver downloads or vendor pip hooks."""
import json
from pathlib import Path
import sys
import shutil
import zipfile
import tempfile

from platformio.package.manager.library import LibraryPackageManager
from platformio.package.manager.platform import PlatformPackageManager
from platformio.package.manager.tool import ToolPackageManager
from platformio.package.meta import PackageSpec

lock = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
cache, runtime = (Path(value) for value in sys.argv[2:])
managers = {"platform": PlatformPackageManager(), "tool": ToolPackageManager(),
            "library": LibraryPackageManager(str(runtime / "libdeps" / lock.get("environment", lock["board"])))}
for item in lock["packages"]:
    archive = item["archive"]
    source = cache / "archives" / (archive["sha256"] + archive["suffix"])
    with tempfile.TemporaryDirectory(prefix="install-", dir=runtime) as temporary:
        if item.get("source_commit"):
            # GitHub's long wrapper directory exceeds Windows limits during PIO extraction.
            normalized = Path(temporary) / "source.zip"
            with zipfile.ZipFile(source) as original, zipfile.ZipFile(normalized, "w") as target:
                if len({name.split("/")[0] for name in original.namelist()}) != 1:
                    raise RuntimeError("Expected a single GitHub archive root")
                for entry in original.infolist():
                    relative = entry.filename.partition("/")[2]
                    if relative:
                        entry.filename = relative
                        target.writestr(entry, original.read(entry.orig_filename))
            source = normalized
        package = managers[item["type"]].install_from_uri("file://" + str(source), PackageSpec(**item["spec"]))
    # Archives without a version otherwise receive a wall-clock-derived version.
    package.metadata.version = item["version"]
    package.dump_meta()
    if Path(package.path).name != item["directory"]:
        raise RuntimeError(f"Unexpected package directory: {package.path}")
    for module in item.get("submodules", []):
        archive = module["archive"]
        source = cache / "archives" / (archive["sha256"] + archive["suffix"])
        root = Path(package.path).resolve()
        destination = (root / module["path"]).resolve()
        if not destination.is_relative_to(root) or destination == root:
            raise RuntimeError("Invalid submodule path")
        with zipfile.ZipFile(source) as bundle:
            prefixes = {name.split("/")[0] for name in bundle.namelist()}
            if len(prefixes) != 1:
                raise RuntimeError("Expected a single GitHub archive root")
            for entry in bundle.infolist():
                relative = entry.filename.partition("/")[2]
                target = (destination / relative).resolve()
                if not target.is_relative_to(destination) or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError("Unsafe submodule archive entry")
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(entry) as inp, target.open("wb") as out:
                        shutil.copyfileobj(inp, out)
    print(f"Installed {item['name']} {item['version']}", flush=True)
