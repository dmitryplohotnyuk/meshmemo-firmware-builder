"""Install verified archives without resolver downloads or vendor pip hooks."""
import json
from pathlib import Path
import sys

from platformio.package.manager.library import LibraryPackageManager
from platformio.package.manager.platform import PlatformPackageManager
from platformio.package.manager.tool import ToolPackageManager
from platformio.package.meta import PackageSpec

lock = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
cache, runtime = (Path(value) for value in sys.argv[2:])
managers = {"platform": PlatformPackageManager(), "tool": ToolPackageManager(),
            "library": LibraryPackageManager(str(runtime / "libdeps" / lock["board"]))}
for item in lock["packages"]:
    archive = item["archive"]
    source = cache / "archives" / (archive["sha256"] + archive["suffix"])
    package = managers[item["type"]].install_from_uri("file://" + str(source), PackageSpec(**item["spec"]))
    # Archives without a version otherwise receive a wall-clock-derived version.
    package.metadata.version = item["version"]
    package.dump_meta()
    if Path(package.path).name != item["directory"]:
        raise RuntimeError(f"Unexpected package directory: {package.path}")
    print(f"Installed {item['name']} {item['version']}", flush=True)
