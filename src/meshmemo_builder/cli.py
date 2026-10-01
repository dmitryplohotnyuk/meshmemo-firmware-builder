"""Command-line interface; preparation never accesses a device."""

import argparse
import json
from pathlib import Path
import shutil
import sys

from . import __version__
from .pipeline import BuilderError, load_catalog, prepare, resolve_plan, run


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="meshmemo-builder")
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("wizard", help="Interactive step-by-step terminal wizard")
    commands.add_parser("list-boards", help="List registered boards and validation status")
    commands.add_parser("list-options", help="List independent optional firmware patches")
    commands.add_parser("doctor", help="Check local Python and Git; does not install tools")
    releases = commands.add_parser("list-releases", help="Discover official Meshtastic releases online")
    releases.add_argument("--channel", choices=("stable", "preview", "all"), default="stable")
    releases.add_argument("--limit", type=int, default=10)
    recovery = commands.add_parser("prepare-recovery", help="Extract a self-contained recovery package from a backup; never flash")
    recovery.add_argument("--backup", type=Path, required=True)
    recovery.add_argument("--destination", type=Path, required=True, help="New directory in an existing parent")
    verify = commands.add_parser("verify-recovery", help="Verify recovery files offline; never access a device")
    verify.add_argument("--package", type=Path, required=True)
    for name in ("inspect-flash", "plan-update"):
        sub = commands.add_parser(name, help="Analyze saved files offline; never access a device")
        sub.add_argument("--backup", type=Path, required=True, help="Full raw flash dump starting at address zero")
        sub.add_argument("--output", type=Path, help="New JSON report file; existing files are never replaced")
        if name == "plan-update":
            sub.add_argument("--manifest", type=Path, required=True, help="Build manifest beside its application image")
            sub.add_argument("--board", required=True, help="Explicit board selection; not inferred from flash contents")
    compile_command = commands.add_parser("build", help="Compile an already prepared workspace; never flash")
    compile_command.add_argument("--workspace", type=Path, required=True)
    compile_command.add_argument("--runtime", type=Path, required=True, help="Runtime created by bootstrap")
    compile_command.add_argument("--output", type=Path, required=True, help="New output directory")
    compile_command.add_argument("--jobs", type=int, default=4)
    for name in ("fetch-dependencies", "bootstrap"):
        sub = commands.add_parser(name, help="Download locked inputs" if name == "fetch-dependencies" else "Install a locked runtime offline")
        sub.add_argument("--workspace", type=Path, required=True)
        sub.add_argument("--cache", type=Path, required=True)
        if name == "bootstrap":
            sub.add_argument("--destination", type=Path, required=True)
    for command in ("inspect", "prepare", "prepare-release"):
        sub = commands.add_parser(command)
        sub.add_argument("--board", default="tbeam-s3-core")
        if command == "prepare-release":
            sub.add_argument("--release", default="latest", help="latest or an exact official release tag")
            sub.add_argument("--channel", choices=("stable", "preview"), default="stable")
            sub.add_argument("--base", default="2.7.26", help="Installed patch baseline")
        else:
            sub.add_argument("--upstream", default="2.7.26")
        sub.add_argument("--profile", default="meshmemo")
        sub.add_argument("--ua22", action="store_true", help="Opt in to custom UA_433 22 dBm ceiling")
        sub.add_argument("--cyrillic", action="store_true", help="Enable OLED_UA and glyph correction")
        sub.add_argument("--display-timeout", action="store_true", help="Allow display timeout during USB sessions")
        sub.add_argument("--windows-workaround", choices=("auto", "on", "off"), default="auto",
                         help="Windows build workaround, independent of firmware options")
        if command in ("prepare", "prepare-release"):
            sub.add_argument("--destination", type=Path, required=True, help="New workspace directory")
            sub.add_argument("--firmware-source", help="Optional local Git repository or HTTPS URL")
            sub.add_argument("--protobuf-source", help="Optional local Git repository or HTTPS URL")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "wizard":
            if not sys.stdin.isatty():
                raise BuilderError("Wizard requires an interactive terminal; use the regular CLI commands for scripts")
            from .wizard import run as wizard
            return wizard()
        elif args.command in ("list-boards", "list-options"):
            key = "boards" if args.command == "list-boards" else "options"
            print(json.dumps(load_catalog()[key], indent=2, ensure_ascii=False))
        elif args.command == "doctor":
            print(f"Python: {sys.version.split()[0]}")
            if not shutil.which("git"):
                raise BuilderError("Git is not available on PATH")
            print(run(["git", "--version"]).strip())
            print("Preparation ready. Locked Windows builds require Python 3.12.14; use fetch-dependencies and bootstrap.")
        elif args.command == "list-releases":
            from .releases import list_releases
            print(json.dumps(list_releases(args.channel, args.limit), indent=2, ensure_ascii=False))
        elif args.command in ("fetch-dependencies", "bootstrap"):
            from .build import read_prepared
            from .environment import bootstrap, fetch, load_lock
            plan = read_prepared(args.workspace.resolve())["plan"]
            if args.command == "fetch-dependencies":
                print(f"Verified dependency cache: {fetch(load_lock(plan), args.cache)}")
            else:
                print(f"Offline runtime ready: {bootstrap(plan, args.cache, args.destination)}")
        elif args.command == "build":
            from .build import build
            path = build(args.workspace, args.runtime, args.output, args.jobs)
            print(f"Built application image. Manifest: {path}. Device not accessed.")
        elif args.command in ("inspect-flash", "plan-update"):
            from .update import inspect_flash, plan_update
            report = (inspect_flash(args.backup) if args.command == "inspect-flash"
                      else plan_update(args.backup, args.manifest, args.board))
            content = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
            if args.output:
                with args.output.open("x", encoding="utf-8", newline="\n") as target:
                    target.write(content)
            else:
                print(content, end="")
            return 2 if report["blockers"] else 0
        elif args.command == "prepare-recovery":
            from .recovery import prepare_recovery
            path = prepare_recovery(args.backup, args.destination)
            print(f"Recovery package prepared: {path}. Contains private flash data; device not accessed.")
        elif args.command == "verify-recovery":
            from .recovery import verify_recovery
            print(json.dumps(verify_recovery(args.package), indent=2))
        else:
            options = [name for name in ("ua22", "cyrillic", "display-timeout")
                       if getattr(args, name.replace("-", "_"))]
            windows = {"auto": None, "on": True, "off": False}[args.windows_workaround]
            if args.command == "prepare-release":
                from .releases import prepare_release
                if args.profile != "meshmemo":
                    raise BuilderError("Unknown profile")
                path = prepare_release(args.destination, args.board, args.release, args.channel, options,
                                       windows, args.base, args.firmware_source, args.protobuf_source)
                print(f"Release sources patched. Manifest: {path}. Compatibility report: {path.parent / 'release.json'}")
                report = json.loads((path.parent / "release.json").read_text(encoding="utf-8"))
                if not report["build_ready"]:
                    print("Sources prepared; compilation blocked until new dependencies are locked and validated.")
                    return 2
                print("Dependency lock compatible. Compilation and hardware validation have not been performed.")
                return 0
            plan = resolve_plan(args.board, args.upstream, args.profile, options, windows)
            if args.command == "inspect":
                print(json.dumps(plan, indent=2, ensure_ascii=False))
            else:
                path = prepare(plan, args.destination, args.firmware_source, args.protobuf_source)
                print(f"Prepared firmware. Manifest: {path}")
        return 0
    except (BuilderError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
