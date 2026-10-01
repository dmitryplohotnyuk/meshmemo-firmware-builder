import hashlib
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from meshmemo_builder import releases
from meshmemo_builder.build import read_prepared
from meshmemo_builder.cli import main
from meshmemo_builder.environment import load_lock
from meshmemo_builder.pipeline import (BuilderError, git,
                                       resolve_plan, resolve_saved_plan, write_json)


def release(tag="v2.8.0.abcdef0", preview=True, published="2026-09-01T10:00:00Z"):
    return dict(tag_name=tag, prerelease=preview, draft=False, published_at=published)


def identity(**changes):
    return {"tag": "v2.8.0.abcdef0", "firmware_commit": "a" * 40, "protobuf_commit": "b" * 40,
            "build_inputs_sha256": releases.tree_digest(releases.baseline_inputs("2.7.26")),
            "base": "2.7.26", **changes}


def test_channels_sort_by_publication_and_exclude_drafts(monkeypatch):
    records = [release(published="2026-08-01T00:00:00Z"), release(preview=False),
               release(tag="v2.8.0.abcdef1"), {**release(), "draft": True},
               release(tag="auxiliary-downloads")]
    monkeypatch.setattr(releases, "api_json", lambda path: records)
    assert [row["tag"] for row in releases.list_releases("preview")] == ["v2.8.0.abcdef1", "v2.8.0.abcdef0"]
    assert len(releases.list_releases("stable")) == 1
    assert len(releases.list_releases("all")) == 3


def test_latest_stable_uses_github_latest_and_pins_commit(monkeypatch):
    paths = []
    def api(path):
        paths.append(path)
        return release(preview=False) if path == "/releases/latest" else {"sha": "a" * 40}
    monkeypatch.setattr(releases, "api_json", api)
    selected = releases.select_release()
    assert selected["firmware_commit"] == "a" * 40
    assert paths == ["/releases/latest", "/commits/v2.8.0.abcdef0"]


def test_preview_requires_explicit_channel(monkeypatch):
    monkeypatch.setattr(releases, "api_json", lambda path: release())
    with pytest.raises(BuilderError, match="channel mismatch"):
        releases.select_release("v2.8.0.abcdef0")


@pytest.mark.parametrize("tag", ["main", "../foo", "--upload-pack=oops", "https://example.org", "a" * 40])
def test_branch_or_injected_release_rejected_before_network(monkeypatch, tag):
    monkeypatch.setattr(releases, "api_json", lambda path: pytest.fail("Network used"))
    with pytest.raises(BuilderError, match="official release tag"):
        releases.select_release(tag)


@pytest.mark.parametrize("error", [HTTPError("https://api.github.com", 403, "rate limit", {}, None),
                                  URLError("offline"), TimeoutError("timed out")])
def test_network_errors_are_actionable_without_fallback(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(releases, "urlopen", fail)
    with pytest.raises(BuilderError, match="GitHub"):
        releases.api_json("/releases/latest")


@pytest.mark.parametrize("bad", [{"tag_name": "../bad"}, {"draft": True}, {"prerelease": "false"},
                                 {"published_at": "bad"}, {"published_at": None}])
def test_bad_release_metadata_rejected(bad):
    with pytest.raises(BuilderError):
        releases.release_summary({**release(), **bad})


def test_candidate_identity_reconstructs_offline_and_reuses_only_matching_lock():
    original = resolve_plan()
    pinned = identity()
    plan = releases.resolve_release_plan("tbeam-s3-core", pinned)
    assert plan["options"] == []
    assert plan["build_id"] != original["build_id"]
    assert resolve_saved_plan(plan) == plan
    assert load_lock(plan)["firmware_commit"] == original["firmware"]["commit"]
    assert "experimental" in plan["hardware"]["status"]["build"]
    changed = releases.resolve_release_plan("tbeam-s3-core", identity(build_inputs_sha256="c" * 64))
    assert changed["dependency_lock"] is None
    with pytest.raises(BuilderError, match="maintainer"):
        load_lock(changed)
    changed["dependency_lock"] = original["dependency_lock"]
    with pytest.raises(BuilderError, match="cannot reuse"):
        load_lock(changed)


@pytest.mark.parametrize("changes", [{"firmware_commit": "main"}, {"protobuf_commit": "bad"},
                                     {"base": "missing"}, {"url": "https://example.org"},
                                     {"tag": "--bad"}, {"build_inputs_sha256": "unknown"}])
def test_untrusted_candidate_metadata_cannot_replace_registry(changes):
    with pytest.raises(BuilderError):
        releases.resolve_release_plan("tbeam-s3-core", identity(**changes))


def test_screen_options_remain_rejected_for_screenless_release():
    with pytest.raises(BuilderError, match="does not support"):
        releases.resolve_release_plan("heltec-wsl-v3", identity(), options=["cyrillic"])


@pytest.mark.parametrize("path", ["platformio.ini", "boards/test.json", "arch/esp32/esp32.ini",
                                 "extra_scripts/new.py", "variants/esp32s3/new/variant.h",
                                 "bin/platformio-custom.py", "lib/new/file.h", "default.csv"])
def test_build_input_boundary_covers_tools_boards_and_dependencies(path):
    assert releases.build_input(path)


def commit(repo):
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def local_release(tmp_path, monkeypatch):
    fw, pb = tmp_path / "firmware-source", tmp_path / "protobuf-source"
    for repo in (fw, pb):
        repo.mkdir()
        git(repo, "init", "-q")
    (pb / "test.proto").write_text('syntax = "proto3";\n')
    pb_commit = commit(pb)
    (fw / "platformio.ini").write_text("[env]\n")
    git(fw, "add", ".")
    git(fw, "update-index", "--add", "--cacheinfo", f"160000,{pb_commit},protobufs")
    git(fw, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
    fw_commit = git(fw, "rev-parse", "HEAD").strip()
    git(fw, "tag", "v9.0.0.fixture")
    selected = {**releases.release_summary(release(tag="v9.0.0.fixture")), "firmware_commit": fw_commit}
    monkeypatch.setattr(releases, "select_release", lambda *args: selected)
    return fw, pb, selected


def test_real_patch_conflict_records_failure_and_preserves_source(tmp_path, local_release):
    fw, pb, _ = local_release
    destination = tmp_path / "candidate"
    with pytest.raises(BuilderError, match="Patch failed"):
        releases.prepare_release(destination, channel="preview", firmware_source=str(fw), protobuf_source=str(pb))
    report = json.loads((destination / "release.json").read_text())
    assert report["status"] == "blocked" and not report["build_ready"]
    assert report["patches"][0]["status"] == "conflict"
    assert report["dependency_status"] == "new-lock-required"
    assert json.loads((destination / "prepare.json").read_text())["status"] == "failed"
    assert (destination / "firmware/platformio.ini").read_text() == "[env]\n"
    with pytest.raises(BuilderError, match="did not complete"):
        read_prepared(destination)
    with pytest.raises(BuilderError, match="already exists"):
        releases.prepare_release(destination)


def test_successful_source_candidate_keeps_unresolved_build_blocked(tmp_path, local_release, monkeypatch):
    fw, pb, _ = local_release
    monkeypatch.setattr("meshmemo_builder.pipeline.apply_plan", lambda *args: None)
    destination = tmp_path / "candidate"
    path = releases.prepare_release(destination, channel="preview", firmware_source=str(fw), protobuf_source=str(pb))
    assert path == destination / "prepare.json"
    state = read_prepared(destination)
    report = json.loads((destination / "release.json").read_text())
    assert state["status"] == "prepared" and report["status"] == "prepared"
    assert not report["build_ready"]
    with pytest.raises(BuilderError, match="dependency lock"):
        load_lock(state["plan"])
    # A forged input digest cannot make the checkout compile with stale locks.
    forged = releases.resolve_release_plan("tbeam-s3-core", {**state["plan"]["release"],
        "build_inputs_sha256": identity()["build_inputs_sha256"]})
    state["plan"] = forged
    write_json(path, state)
    with pytest.raises(BuilderError, match="fingerprint mismatch"):
        read_prepared(destination)


def test_moved_tag_is_rejected_before_patching(tmp_path, local_release):
    fw, pb, selected = local_release
    selected["firmware_commit"] = git(fw, "rev-parse", "HEAD").strip()
    (fw / "another.txt").write_text("new commit")
    commit(fw)
    git(fw, "tag", "-f", selected["tag"])
    with pytest.raises(BuilderError, match="tag moved"):
        releases.prepare_release(tmp_path / "candidate", channel="preview", firmware_source=str(fw), protobuf_source=str(pb))


def test_cli_failure_and_listing(monkeypatch, capsys):
    monkeypatch.setattr(releases, "list_releases", lambda channel, limit: [{"channel": channel}])
    assert main(["list-releases", "--channel", "preview"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"channel": "preview"}]
    assert main(["prepare-release", "--board", "heltec-wsl-v3", "--cyrillic", "--destination", "unused"]) == 1
    assert "does not support" in capsys.readouterr().err


def test_existing_registered_plan_ids_remain_unchanged():
    plan = resolve_plan(board="heltec-wsl-v3", windows_workaround=True)
    assert plan["build_id"] == "0b42e055bf58e3c247c376b596c48d92dd4b545c3ab68536230ae7ef0e150558"


def test_experimental_image_can_be_inspected_but_not_planned_for_writing(tmp_path):
    from test_update import application, flash_file
    from meshmemo_builder.update import candidate, plan_update
    plan = releases.resolve_release_plan("tbeam-s3-core", identity(), windows_workaround=False)
    raw = application(bytes.fromhex(plan["build_id"]))
    (tmp_path / "application.bin").write_bytes(raw)
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {"status": "built", "artifact_kind": "application-only", "plan": plan,
                         "artifact": "application.bin", "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)})
    assert candidate(manifest, "tbeam-s3-core")[0]["plan"] == plan
    report = plan_update(flash_file(tmp_path), manifest, "tbeam-s3-core")
    assert report["status"] == "blocked" and report["proposed_write"] is None
    assert "Experimental release" in report["blockers"][0]


@pytest.mark.parametrize("options", [[], ["ua22", "cyrillic", "display-timeout"]])
def test_actual_patches_on_new_compatible_commit(tmp_path, monkeypatch, options):
    source = os.environ.get("MESHMEMO_TEST_FIRMWARE")
    protobufs = os.environ.get("MESHMEMO_TEST_PROTOBUFS")
    if not source or not protobufs:
        pytest.skip("Set upstream repositories for real patch integration")
    from meshmemo_builder.pipeline import clone
    original = resolve_plan(options=options, windows_workaround=True)
    upstream = tmp_path / "upstream"
    clone(source, upstream, original["firmware"]["commit"])
    (upstream / "release-test.txt").write_text("Synthetic release fixture, not an official release.\n")
    firmware_commit = commit(upstream)
    git(upstream, "tag", "v9.0.0.fixture")
    selected = {**releases.release_summary(release(tag="v9.0.0.fixture")), "firmware_commit": firmware_commit}
    monkeypatch.setattr(releases, "select_release", lambda *args: selected)
    destination = tmp_path / "prepared"
    releases.prepare_release(destination, channel="preview", options=options, windows_workaround=True,
                             firmware_source=str(upstream), protobuf_source=protobufs)
    state = read_prepared(destination)
    plan = state["plan"]
    assert plan["firmware"]["commit"] == firmware_commit and plan["build_id"] != original["build_id"]
    assert plan["options"] == sorted(options)
    assert load_lock(plan)["firmware_commit"] == original["firmware"]["commit"]
    report = json.loads((destination / "release.json").read_text())
    assert report["build_ready"] and report["registered_upstream"] is None
    assert all(row["status"] == "applied" for row in report["patches"])
    bridge = (destination / "firmware/src/mesh/UsbSfBridge.cpp").read_text()
    assert f'memcpy(reply + 28, "{firmware_commit}", 40);' in bridge
    assert original["firmware"]["commit"] not in bridge
