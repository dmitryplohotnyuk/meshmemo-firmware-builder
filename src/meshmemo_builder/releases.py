"""Discover official releases and prepare pinned, explicitly experimental sources."""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .pipeline import (BuilderError, clone, contained, data_root, git, load_catalog,
                       plan_identity, resolve_plan, sha256, write_json)

API = "https://api.github.com/repos/meshtastic/firmware"
TAG = re.compile(r"v?\d+\.\d+\.\d+(?:[.\-][A-Za-z0-9]+)*")
SHA = re.compile(r"[0-9a-f]{40}")


def api_json(path):
    request = Request(API + path, headers={"User-Agent": "MeshMemo-Firmware-Builder",
                      "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise BuilderError("GitHub response is too large")
        return json.loads(raw)
    except HTTPError as exc:
        reason = " (rate limit or access denied; retry later)" if exc.code in (403, 429) else ""
        raise BuilderError(f"GitHub release request failed: HTTP {exc.code}{reason}") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise BuilderError(f"Cannot read official GitHub releases: {exc}") from exc


def release_summary(value):
    try:
        tag = value["tag_name"]
        if (not isinstance(tag, str) or not TAG.fullmatch(tag)
                or type(value["prerelease"]) is not bool or type(value["draft"]) is not bool
                or value["draft"]):
            raise ValueError("unsupported tag or unpublished release")
        published = value["published_at"]
        if not isinstance(published, str):
            raise ValueError("missing publication date")
        if datetime.fromisoformat(published.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("publication date must include a timezone")
        return {"tag": tag, "prerelease": value["prerelease"], "published_at": published,
                "url": f"https://github.com/meshtastic/firmware/releases/tag/{tag}"}
    except (KeyError, TypeError, ValueError) as exc:
        raise BuilderError(f"Invalid official release metadata: {exc}") from exc


def list_releases(channel="stable", limit=10):
    if channel not in ("stable", "preview", "all") or not 1 <= limit <= 100:
        raise BuilderError("Use stable, preview or all, with a limit between 1 and 100")
    result = []
    # GitHub orders by creation, which need not equal publication time. Sort the
    # bounded discovery window explicitly and never silently switch channels.
    for page in range(1, 4):
        records = api_json(f"/releases?per_page=100&page={page}")
        if not isinstance(records, list):
            raise BuilderError("Invalid GitHub releases response")
        for item in records:
            if not isinstance(item, dict):
                raise BuilderError("Invalid GitHub release entry")
            tag = item.get("tag_name")
            # Historical auxiliary releases may use non-firmware tags.
            if item.get("draft") or not isinstance(tag, str) or not TAG.fullmatch(tag):
                continue
            summary = release_summary(item)
            if channel == "all" or summary["prerelease"] == (channel == "preview"):
                result.append(summary)
        if len(records) < 100:
            break
    result.sort(key=lambda row: datetime.fromisoformat(row["published_at"].replace("Z", "+00:00")), reverse=True)
    return result[:limit]


def select_release(release="latest", channel="stable"):
    if channel not in ("stable", "preview"):
        raise BuilderError("Release channel must be stable or preview")
    if release == "latest":
        if channel == "stable":
            selected = release_summary(api_json("/releases/latest"))
        else:
            candidates = list_releases("preview", 1)
            if not candidates:
                raise BuilderError("No published preview release found in the last 300 releases")
            selected = candidates[0]
    else:
        if not TAG.fullmatch(release):
            raise BuilderError("Specify an official release tag, not a branch, URL or commit")
        selected = release_summary(api_json("/releases/tags/" + quote(release, safe="")))
        if selected["tag"] != release:
            raise BuilderError("GitHub returned a different release tag")
    if selected["prerelease"] != (channel == "preview"):
        raise BuilderError("Release channel mismatch; preview releases require --channel preview")
    try:
        commit = api_json("/commits/" + quote(selected["tag"], safe=""))["sha"]
        if not isinstance(commit, str) or not SHA.fullmatch(commit):
            raise ValueError("invalid commit")
    except (KeyError, TypeError, ValueError) as exc:
        raise BuilderError("GitHub did not resolve the release tag to a full commit") from exc
    return {**selected, "firmware_commit": commit}


def build_input(path):
    """Conservative boundary for reusing the installed dependency lock."""
    part = PurePosixPath(path)
    return (part.parts[0] in ("bin", "boards", "lib", "extra_scripts", "arch", "variants")
            or (part.parts[0] not in (".github", "docs", "test", "tests")
                and part.suffix.lower() in (".ini", ".py", ".json", ".toml", ".csv", ".ld", ".cmake"))
            or path in (".gitmodules", "CMakeLists.txt", "requirements.txt"))


def input_tree(firmware, commit):
    result = {}
    for record in git(firmware, "ls-tree", "-r", "-z", commit).split("\0"):
        if record:
            metadata, path = record.split("\t", 1)
            if build_input(path):
                result[path] = metadata
    return result


def tree_digest(tree):
    return hashlib.sha256(json.dumps(tree, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def baseline_inputs(base, root=None):
    root = root or data_root()
    catalog = load_catalog(root)
    try:
        asset = catalog["release_bases"][base]
        path = contained(root, asset["path"])
        if sha256(path) != asset["sha256"]:
            raise BuilderError("Release baseline checksum mismatch")
        value = json.loads(path.read_text(encoding="utf-8"))
        if value["firmware_commit"] != catalog["upstreams"][base]["firmware"]["commit"]:
            raise BuilderError("Release baseline does not match the installed registry")
        return value["build_inputs"]
    except (KeyError, ValueError, TypeError) as exc:
        raise BuilderError(f"Missing or invalid release baseline: {base}") from exc


def resolve_release_plan(board, release, options=(), windows_workaround=None, base="2.7.26", root=None,
                         profile="meshmemo"):
    """Reconstruct only from known assets plus pinned official-source identities."""
    root = root or data_root()
    required = {"tag", "firmware_commit", "protobuf_commit", "build_inputs_sha256", "base"}
    if (not isinstance(release, dict) or set(release) != required or release["base"] != base
            or not isinstance(release["tag"], str) or not TAG.fullmatch(release["tag"])
            or any(not isinstance(release[key], str) or not SHA.fullmatch(release[key])
                   for key in ("firmware_commit", "protobuf_commit"))
            or not isinstance(release["build_inputs_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", release["build_inputs_sha256"])):
        raise BuilderError("Invalid pinned release identity")
    plan = deepcopy(resolve_plan(board, base, profile, options, windows_workaround, root))
    baseline = baseline_inputs(base, root)
    plan["upstream"] = release["tag"]
    plan["release"] = dict(release)
    plan["firmware"]["commit"] = release["firmware_commit"]
    plan["protobufs"]["commit"] = release["protobuf_commit"]
    if release["build_inputs_sha256"] != tree_digest(baseline):
        plan["dependency_lock"] = None
    plan["hardware"]["status"] = {key: "experimental-not-validated" for key in ("prepare", "build", "usb", "radio")}
    plan["build_id"] = plan_identity(plan)
    return plan


def validate_sources(plan, firmware):
    release = plan["release"]
    if git(firmware, "rev-parse", "HEAD").strip() != release["firmware_commit"]:
        raise BuilderError("Release checkout commit changed")
    if git(firmware, "rev-parse", f"refs/tags/{release['tag']}^{{commit}}").strip() != release["firmware_commit"]:
        raise BuilderError("Release tag moved or does not match the pinned commit")
    record = git(firmware, "ls-tree", "HEAD", "protobufs").strip()
    if record != f"160000 commit {release['protobuf_commit']}\tprotobufs":
        raise BuilderError("Protobuf commit does not match the firmware gitlink")
    if git(firmware / "protobufs", "rev-parse", "HEAD").strip() != release["protobuf_commit"]:
        raise BuilderError("Protobuf checkout commit changed")
    if tree_digest(input_tree(firmware, "HEAD")) != release["build_inputs_sha256"]:
        raise BuilderError("Release build-input fingerprint mismatch")


def prepare_release(destination, board="tbeam-s3-core", release="latest", channel="stable",
                    options=(), windows_workaround=None, base="2.7.26",
                    firmware_source=None, protobuf_source=None, profile="meshmemo"):
    # Resolve board/options before any network access. Existing directories never
    # become scratch space, even if discovery or an earlier attempt failed.
    template = resolve_plan(board, base, profile, options, windows_workaround)
    destination = destination.resolve()
    if destination.exists() or destination.is_symlink():
        raise BuilderError("Destination already exists; choose a new directory")
    selected = select_release(release, channel)
    destination.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": 1, "status": "checking", "release": selected,
              "board": board, "profile": profile, "options": template["options"], "base": base,
              "patches": [{"path": item["path"], "target": item["target"], "status": "not-run"}
                          for item in template["patches"]],
              "build": "not-run", "hardware": "not-tested"}
    report_path = destination / "release.json"
    write_json(report_path, report)
    stage = "download-firmware"
    try:
        firmware = destination / "firmware"
        clone(firmware_source or template["firmware"]["url"], firmware, selected["firmware_commit"])
        if git(firmware, "rev-parse", f"refs/tags/{selected['tag']}^{{commit}}").strip() != selected["firmware_commit"]:
            raise BuilderError("Release tag moved or does not match the pinned commit")
        record = git(firmware, "ls-tree", "HEAD", "protobufs").strip().split()
        if len(record) != 4 or record[:2] != ["160000", "commit"] or not SHA.fullmatch(record[2]):
            raise BuilderError("Release has no supported protobufs submodule")
        protobuf_commit = record[2]
        stage = "download-protobufs"
        clone(protobuf_source or template["protobufs"]["url"], firmware / "protobufs", protobuf_commit)
        for repo in ("firmware", "protobufs"):
            git(firmware if repo == "firmware" else firmware / "protobufs", "remote", "set-url", "origin", template[repo]["url"])
        inputs = input_tree(firmware, "HEAD")
        baseline = baseline_inputs(base)
        changed = sorted(path for path in inputs.keys() | baseline.keys() if inputs.get(path) != baseline.get(path))
        report["dependency_changes"] = changed
        report["dependency_status"] = "new-lock-required" if changed else "baseline-lock-compatible"
        pinned = {"tag": selected["tag"], "firmware_commit": selected["firmware_commit"],
                  "protobuf_commit": protobuf_commit, "build_inputs_sha256": tree_digest(inputs), "base": base}
        # An exact already-registered release retains its original plan/build ID.
        known = next((key for key, item in load_catalog()["upstreams"].items()
                      if item["firmware"]["commit"] == pinned["firmware_commit"]
                      and item["protobufs"]["commit"] == pinned["protobuf_commit"]
                      and key in template["hardware"]["upstreams"]), None)
        plan = (resolve_plan(board, known, profile, options, windows_workaround) if known
                else resolve_release_plan(board, pinned, options, windows_workaround, base, profile=profile))
        report["registered_upstream"] = known
        report["pinned"] = pinned
        report["patches"] = [{"path": item["path"], "target": item["target"], "status": "not-run"}
                             for item in plan["patches"]]
        write_json(report_path, report)
        stage = "patches"
        def progress(item, status, error=None):
            row = next(row for row in report["patches"] if row["path"] == item["path"] and row["target"] == item["target"])
            row["status"] = status
            if error:
                row["error"] = error
            write_json(report_path, report)
        from .pipeline import finish_prepare
        result = finish_prepare(plan, destination, patch_progress=progress)
        report["status"] = "prepared"
        report["build_ready"] = plan["dependency_lock"] is not None
        write_json(report_path, report)
        return result
    except (BuilderError, OSError) as exc:
        report.update(status="blocked", failed_stage=stage, error=str(exc), build_ready=False)
        write_json(report_path, report)
        raise BuilderError(f"Release preparation blocked at {stage}. Report: {report_path}\n{exc}") from exc
