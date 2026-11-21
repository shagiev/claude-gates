"""GitHub Release deploy: docs/2026-11-10-github-release-deploy-design.md, R1–R42."""
import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import deploy_gates as dg  # noqa: E402
import deploy_verify as dv  # noqa: E402
import github_release as gr  # noqa: E402
from test_deploy import harness, install  # noqa: E402,F401


SHA = "a" * 40
OLD_SHA = "b" * 40
REAL_SHA = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                          capture_output=True, text=True, check=True).stdout.strip()
REAL_VERSION = json.loads(subprocess.run(
    ["git", "show", f"{REAL_SHA}:plugins/gates/.kimi-plugin/plugin.json"], cwd=ROOT,
    capture_output=True, text=True, check=True).stdout)["version"]


def _zip(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as bundle:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            bundle.writestr(info, data)
    return out.getvalue()


def _valid_zip(version="9.9.9"):
    return _zip([
        (".kimi-plugin/plugin.json", json.dumps({"version": version}).encode(), 0o100644),
        (".claude-plugin/plugin.json", json.dumps({"version": version}).encode(), 0o100644),
        ("scripts/gate.py", b"gate\n", 0o100755),
    ])


def _tree(root):
    result = {}
    for path in root.rglob("*"):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = " ".join(dv.blob_entry(path))
    return result


def test_r35_release_url_is_derived_only_from_a_strict_version():
    identity = gr.release_identity("1.2.3")
    assert identity == {
        "version": "1.2.3", "tag": "v1.2.3", "asset": "gates-1.2.3.zip",
        "url": "https://github.com/shagiev/claude-gates/releases/download/v1.2.3/gates-1.2.3.zip",
        "checksum_asset": "gates-1.2.3.zip.sha256",
    }
    for invalid in ("1.2", "1.2.3+local", "v1.2.3", "1.2.3/../../x", "１.2.3", "1.2.3?x=1"):
        with pytest.raises(gr.ReleaseError):
            gr.release_identity(invalid)


def test_r35_arbitrary_sources_never_reach_kimi_install(monkeypatch):
    calls = []
    monkeypatch.setattr(dv.ka, "_api", lambda *args: calls.append(args))
    for source in ("/tmp/gates", "file:///tmp/gates.zip", "https://fork/x.zip",
                   "https://user@github.com/shagiev/claude-gates/x.zip",
                   gr.release_identity("1.2.3")["url"] + "?raw=1"):
        with pytest.raises(dv.InstallError):
            dv.cmd_update(json.dumps({"version": "1.2.3", "source": source}))
    assert calls == []


def test_r4_archive_rejects_unsafe_or_wrong_layout(tmp_path):
    required = [(".kimi-plugin/plugin.json", b'{"version":"9.9.9"}', 0o100644),
                (".claude-plugin/plugin.json", b'{"version":"9.9.9"}', 0o100644)]
    archives = [
        _zip([("plugins/gates/.kimi-plugin/plugin.json", b"{}", 0o100644)]),
        _zip([("../escape", b"x", 0o100644), *required]),
        _zip([("/absolute", b"x", 0o100644), *required]),
        _zip([("scripts/__pycache__/x.pyc", b"x", 0o100644), *required]),
        _zip([("scripts/link", b"../../outside", 0o120777), *required]),
    ]
    for index, data in enumerate(archives):
        with pytest.raises(gr.ReleaseError):
            gr.inspect_archive(data, tmp_path / f"out-{index}")


def test_r4_publisher_enforces_entry_and_cumulative_extract_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(gr, "_MAX_ENTRY_BYTES", 100)
    with pytest.raises(gr.ReleaseError, match="entry"):
        gr.inspect_archive(_zip([("big", b"x" * 101, 0o100644)]), tmp_path / "entry")
    monkeypatch.setattr(gr, "_MAX_EXTRACT_BYTES", 150)
    with pytest.raises(gr.ReleaseError, match="total"):
        gr.inspect_archive(_zip([("a", b"x" * 100, 0o100644),
                                 ("b", b"y" * 100, 0o100644)]), tmp_path / "total")


def test_r4_host_enforces_entry_and_cumulative_extract_limits(tmp_path, monkeypatch):
    source = gr.release_identity("9.9.9")["url"]
    monkeypatch.setattr(dv, "_MAX_ENTRY_BYTES", 100)
    monkeypatch.setattr(dv.ka.urllib.request, "urlopen", lambda *args, **kwargs:
                        io.BytesIO(_zip([("big", b"x" * 101, 0o100644)])))
    with pytest.raises(dv.ReleaseArchiveError, match="entry"):
        dv._extract_release(source, "9.9.9", tmp_path / "entry")
    monkeypatch.setattr(dv, "_MAX_EXTRACT_BYTES", 150)
    monkeypatch.setattr(dv.ka.urllib.request, "urlopen", lambda *args, **kwargs:
                        io.BytesIO(_zip([("a", b"x" * 100, 0o100644),
                                         ("b", b"y" * 100, 0o100644)])))
    with pytest.raises(dv.ReleaseArchiveError, match="total"):
        dv._extract_release(source, "9.9.9", tmp_path / "total")


def test_r4_host_rejects_duplicate_zip_names(tmp_path, monkeypatch):
    data = _zip([("same", b"first", 0o100644), ("same", b"second", 0o100644)])
    monkeypatch.setattr(dv.ka.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(data))
    with pytest.raises(dv.ReleaseArchiveError, match="duplicate"):
        dv._extract_release(gr.release_identity("9.9.9")["url"], "9.9.9", tmp_path / "plugin")


def test_r14_source_tree_is_artifact_but_download_outage_is_not(tmp_path, monkeypatch):
    data = _valid_zip()
    extracted = gr.inspect_archive(data, tmp_path / "expected", "9.9.9")
    want = {"version": "9.9.9", "source": gr.release_identity("9.9.9")["url"],
            "tree": _tree(extracted), "enabled": False, "checks": ["source"]}
    want["tree"]["scripts/gate.py"] = "100755 " + "f" * 40
    monkeypatch.setattr(dv, "installed", lambda _ch: {
        "state": "installed", "version": "9.9.9", "source": want["source"],
        "path": str(extracted), "enabled": False})
    monkeypatch.setattr(dv.ka.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(data))
    report = dv.cmd_verify(json.dumps({"channels": {"kimi": want}}))
    assert [problem["code"] for problem in report["problems"]] == ["source-tree"]
    assert "source-tree" in dg._ARTIFACT_PROBLEM_CODES

    def outage(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(dv.ka.urllib.request, "urlopen", outage)
    report = dv.cmd_verify(json.dumps({"channels": {"kimi": want}}))
    assert [problem["code"] for problem in report["problems"]] == ["source"]
    assert "source" not in dg._ARTIFACT_PROBLEM_CODES


def test_r3_real_git_archive_is_repeatable_and_matches_commit_tree(tmp_path):
    artifact = gr.build_release_artifact(REAL_SHA, REAL_VERSION, dg.expected_tree(REAL_SHA), tmp_path)
    again = gr.build_release_artifact(REAL_SHA, REAL_VERSION, dg.expected_tree(REAL_SHA),
                                      tmp_path / "again")
    assert artifact["zip_sha256"] == again["zip_sha256"]
    assert artifact["tree"] == dg.expected_tree(REAL_SHA)
    assert artifact["digest"] == dv.tree_digest(artifact["tree"])
    assert hashlib.sha256(Path(artifact["zip"]).read_bytes()).hexdigest() == artifact["zip_sha256"]


def test_r3_build_uses_binary_trusted_git_not_working_tree(monkeypatch, tmp_path):
    data = _valid_zip()
    calls = []

    def git_bytes(*args, cwd=None):
        calls.append(args)
        payload = b"1794292116\n" if args[0] == "show" else data
        return subprocess.CompletedProcess(args, 0, payload, b"")

    monkeypatch.setattr(gr.g, "_trusted_git_bytes", git_bytes)
    monkeypatch.setattr(gr, "expected_disk_tree", lambda _root: {
        ".kimi-plugin/plugin.json": "100644 " + "0" * 40,
        ".claude-plugin/plugin.json": "100644 " + "1" * 40,
        "scripts/gate.py": "100755 " + "2" * 40,
    })
    want = {
        ".kimi-plugin/plugin.json": "100644 " + "0" * 40,
        ".claude-plugin/plugin.json": "100644 " + "1" * 40,
        "scripts/gate.py": "100755 " + "2" * 40,
    }
    artifact = gr.build_release_artifact(SHA, "9.9.9", want, tmp_path)
    assert calls == [("show", "-s", "--format=%ct", SHA),
                     ("archive", "--format=zip", "--mtime=@1794292116",
                      f"{SHA}:plugins/gates")] * 2
    assert artifact["zip_sha256"] == hashlib.sha256(data).hexdigest()


class FakeGh:
    def __init__(self, release=None):
        self.release = release
        self.calls = []
        self.uploads = []

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["release", "view"]:
            if self.release is None:
                return subprocess.CompletedProcess(args, 1, "", "release not found")
            payload = {"isDraft": False, "isPrerelease": True, **self.release}
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
        if args[:2] == ["release", "create"]:
            self.release = {"tagName": args[2], "assets": []}
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["release", "upload"]:
            self.uploads.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "api":
            return subprocess.CompletedProcess(args, 0, SHA + "\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")


def _artifact(tmp_path):
    data = _valid_zip()
    path = tmp_path / "gates-9.9.9.zip"
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    checksum = tmp_path / "gates-9.9.9.zip.sha256"
    checksum.write_text(f"{digest}  gates-9.9.9.zip\n")
    return {**gr.release_identity("9.9.9"), "sha": SHA, "zip": str(path),
            "checksum": str(checksum), "zip_sha256": digest, "zip_bytes": data,
            "tree": {"x": "100644 " + "0" * 40}, "digest": "tree-digest"}


def test_r5_new_release_is_prerelease_not_latest_and_has_two_assets(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path)
    gh = FakeGh()
    monkeypatch.setattr(gr, "download_and_verify", lambda *args, **kwargs: None)
    gr.publish_release(artifact, gh=gh)
    create = next(call for call in gh.calls if call[:2] == ["release", "create"])
    assert "--prerelease" in create and "--latest=false" in create
    assert "--target" in create and SHA in create
    assert str(tmp_path / "gates-9.9.9.zip.sha256") in create
    assert not any("--clobber" in call for call in gh.calls)


def test_r6_identical_release_is_not_rewritten(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path)
    gh = FakeGh({"tagName": "v9.9.9", "assets": [
        {"name": "gates-9.9.9.zip"}, {"name": "gates-9.9.9.zip.sha256"}]})
    monkeypatch.setattr(gr, "download_and_verify", lambda *args, **kwargs: None)
    gr.publish_release(artifact, gh=gh)
    assert not any(call[:2] in (["release", "create"], ["release", "upload"]) for call in gh.calls)


def test_r7_existing_tag_on_another_commit_is_fatal(tmp_path):
    artifact = _artifact(tmp_path)
    gh = FakeGh({"tagName": "v9.9.9", "assets": []})
    original = gh.__call__

    def run(args):
        if args[0] == "api":
            return subprocess.CompletedProcess(args, 0, OLD_SHA + "\n", "")
        return original(args)

    with pytest.raises(gr.ReleaseError, match="tag"):
        gr.publish_release(artifact, gh=run)


def test_r7_malformed_tag_target_is_fatal(tmp_path):
    artifact = _artifact(tmp_path)
    gh = FakeGh({"tagName": "v9.9.9", "assets": []})
    original = gh.__call__

    def run(args):
        if args[0] == "api":
            return subprocess.CompletedProcess(args, 0, "not-a-sha\n", "")
        return original(args)

    with pytest.raises(gr.ReleaseError, match="полным commit SHA"):
        gr.publish_release(artifact, gh=run)


def test_r7_new_release_rechecks_preexisting_tag_and_drafts_mismatch(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path)
    gh = FakeGh()
    original = gh.__call__

    def run(args):
        if args[0] == "api":
            return subprocess.CompletedProcess(args, 0, OLD_SHA + "\n", "")
        return original(args)

    monkeypatch.setattr(gr, "download_and_verify", lambda *args, **kwargs:
                        pytest.fail("download продолжился после tag mismatch"))
    with pytest.raises(gr.ReleaseError, match="tag"):
        gr.publish_release(artifact, gh=run)
    assert any(call[:2] == ["release", "edit"] and "--draft=true" in call for call in gh.calls)


def test_r8_r9_missing_asset_is_added_but_conflicting_or_clobber_is_not(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path)
    gh = FakeGh({"tagName": "v9.9.9", "assets": [{"name": "gates-9.9.9.zip"}]})
    monkeypatch.setattr(gr, "download_and_verify", lambda *args, **kwargs: None)
    gr.publish_release(artifact, gh=gh)
    assert len(gh.uploads) == 1
    assert "gates-9.9.9.zip.sha256" in " ".join(gh.uploads[0])
    assert all("--clobber" not in call for call in gh.calls)


def test_r9_existing_assets_are_verified_before_missing_upload(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path)
    gh = FakeGh({"tagName": "v9.9.9", "assets": [{"name": "gates-9.9.9.zip"}]})
    calls = []

    def verify(*args, **kwargs):
        calls.append((kwargs.get("verify_zip", True), kwargs.get("verify_checksum", True)))

    monkeypatch.setattr(gr, "download_and_verify", verify)
    gr.publish_release(artifact, gh=gh)
    assert calls == [(True, False), (True, True)]
    assert len(gh.uploads) == 1


def test_r9_corrupt_existing_asset_blocks_missing_upload(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path)
    gh = FakeGh({"tagName": "v9.9.9", "assets": [{"name": "gates-9.9.9.zip"}]})
    monkeypatch.setattr(gr, "download_and_verify", lambda *args, **kwargs:
                        (_ for _ in ()).throw(gr.ReleaseError("corrupt existing asset")))
    with pytest.raises(gr.ReleaseError, match="corrupt existing"):
        gr.publish_release(artifact, gh=gh)
    assert gh.uploads == []


def test_r45_source_failure_never_runs_managed_smoke(tmp_path, monkeypatch):
    canonical = gr.release_identity("9.9.9")["url"]
    ran = []
    monkeypatch.setattr(dv, "installed", lambda _ch: {
        "state": "installed", "version": "9.9.9", "source": canonical,
        "path": str(tmp_path), "enabled": False})
    monkeypatch.setattr(dv, "_verify_kimi_source", lambda *args:
                        (_ for _ in ()).throw(dv.ReleaseDigestError("wrong digest")))
    monkeypatch.setattr(dv, "SMOKES", (lambda _root: ran.append(1),))
    report = dv.cmd_verify(json.dumps({"channels": {"kimi": {
        "version": "9.9.9", "source": canonical, "tree": {}, "enabled": False,
        "checks": ["source", "smoke"]}}}))
    assert [p["code"] for p in report["problems"]] == ["zip-sha256"]
    assert ran == []


def test_r10_downloaded_zip_must_match_publisher_digest(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path)
    monkeypatch.setattr(gr, "_download", lambda _url: b"substituted")
    with pytest.raises(gr.ReleaseError, match="ZIP"):
        gr.download_and_verify(artifact)


def test_r10_publisher_zip_digest_reaches_every_host_verifier(harness):
    calls = []
    harness.setattr(dg, "verify", lambda *args: calls.append(args) or {
        "ok": True, "problems": [], "channels": {}})
    assert dg.main([]) == 0
    assert len(calls) == 2
    assert all(call[-1] == "0" * 64 for call in calls)


def test_r10_host_zip_must_match_publisher_digest(tmp_path, monkeypatch):
    data = _valid_zip()
    extracted = gr.inspect_archive(data, tmp_path / "expected", "9.9.9")
    want = {"version": "9.9.9", "source": gr.release_identity("9.9.9")["url"],
            "tree": _tree(extracted), "zip_sha256": hashlib.sha256(data).hexdigest()}
    monkeypatch.setattr(dv.ka.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(data))
    assert dv._verify_kimi_source(want) == []
    want["zip_sha256"] = "0" * 64
    with pytest.raises(dv.ReleaseDigestError, match="SHA-256"):
        dv._verify_kimi_source(want)
    monkeypatch.setattr(dv, "installed", lambda _ch: {
        "state": "installed", "version": "9.9.9", "source": want["source"],
        "path": str(extracted), "enabled": False})
    report = dv.cmd_verify(json.dumps({"channels": {"kimi": {
        **want, "enabled": False, "checks": ["source"]}}}))
    assert [problem["code"] for problem in report["problems"]] == ["zip-sha256"]


def test_r11_downloaded_tree_must_match_publisher_commit_tree(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path)
    monkeypatch.setattr(gr, "_download", lambda _url: artifact["zip_bytes"])
    with pytest.raises(gr.ReleaseError, match="дерев"):
        gr.download_and_verify(artifact)


def test_remote_agent_source_executes_and_helper_has_file(monkeypatch, tmp_path):
    def show(*args, cwd=None):
        name = args[1].split(":scripts/", 1)[1]
        return subprocess.CompletedProcess(args, 0, (ROOT / "scripts" / name).read_text(), "")

    monkeypatch.setattr(dg.g, "_trusted_git", show)
    monkeypatch.setattr(dg, "_AGENT_SRC", None)
    dg.load_agent(SHA)
    assert "_deploy_ka.__file__ = 'kimi_acceptance.py'" in dg._AGENT_SRC
    result = subprocess.run(["python3", "-B", "-", "installed", "claude"],
                            input=dg._AGENT_SRC, capture_output=True, text=True,
                            cwd=tmp_path, env={**os.environ, "HOME": str(tmp_path),
                                               "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["channels"]["claude"]["state"] == "absent"


def test_r38_failed_release_becomes_draft_and_full_install_becomes_latest(tmp_path):
    artifact = _artifact(tmp_path)
    calls = []
    gr.mark_release_draft(artifact, gh=lambda args: calls.append(args) or
                          subprocess.CompletedProcess(args, 0, "", ""))
    gr.mark_release_latest(artifact, gh=lambda args: calls.append(args) or
                           subprocess.CompletedProcess(args, 0, "", ""))
    assert "--draft=true" in calls[0]
    assert "--latest" in calls[1] and "--prerelease=false" in calls[1]


def test_r1_r39_review_evidence_is_checked_before_release(harness, monkeypatch):
    events = []
    harness.setattr(dg, "check_review_evidence", lambda sha: (_ for _ in ()).throw(SystemExit(1)))
    harness.setattr(dg, "build_and_publish", lambda pin: events.append("release"))
    with pytest.raises(SystemExit):
        dg.main([])
    assert events == []


def test_r25_inventory_requires_kimi_and_all_channels_are_explicit():
    hosts = dg.read_inventory()
    assert hosts and all("kimi" in host["channels"] for host in hosts)
    assert set(dv.CHANNELS) == {"claude", "codex", "kimi"}
    assert dv.installed("unknown")["state"] == "undetermined"


def test_r31_local_kimi_requires_a_verified_canonical_rollback_asset(monkeypatch):
    calls = []
    monkeypatch.setattr(dg, "agent", lambda h, cmd, arg: calls.append(json.loads(arg)) or
                        {"ok": False, "problems": [{"channel": "kimi", "code": "source",
                                                    "text": "release not found"}]})
    why = dg.check_rollback_target("h", {"channels": {"kimi": {
        "state": "installed", "version": "1.0.0", "source": "/local/gates",
        "tree": {"scripts/x.py": "100644 " + "0" * 40}, "enabled": True}}})
    assert why and "release not found" in why
    spec = calls[0]["channels"]["kimi"]
    assert spec["source"] == gr.release_identity("1.0.0")["url"]
    assert spec["migration"] is True


def test_r35_new_install_cannot_be_sneaked_in_from_a_local_path(monkeypatch, tmp_path):
    canonical = gr.release_identity("1.0.0")["url"]
    monkeypatch.setattr(dv, "installed", lambda _ch: {
        "state": "installed", "version": "1.0.0", "source": "/local/gates",
        "path": str(tmp_path), "enabled": True, "commandCount": 5, "hookCount": 3})
    def verified_source(_want, observed):
        observed["zip_sha256"] = "0" * 64
        return []

    monkeypatch.setattr(dv, "_verify_kimi_source", verified_source)
    report = dv.cmd_verify(json.dumps({"channels": {"kimi": {
        "version": "1.0.0", "source": canonical, "checks": ["source"]}}}))
    assert not report["ok"] and report["problems"][0]["code"] == "source"


def test_r31_rollback_asset_is_checked_against_snapshot_tree_before_mutation(monkeypatch):
    calls = []
    snap = {"channels": {"kimi": {
        "state": "installed", "version": "1.0.0", "source": gr.release_identity("1.0.0")["url"],
        "tree": {"scripts/x.py": "100644 " + "0" * 40}, "enabled": True}}}
    monkeypatch.setattr(dg, "agent", lambda h, cmd, arg: calls.append((cmd, json.loads(arg))) or
                        {"ok": False, "problems": [{"channel": "kimi", "code": "source",
                                                    "text": "digest mismatch"}]})
    assert dg.check_rollback_target("h", snap)
    assert calls[0][0] == "verify"
    assert calls[0][1]["channels"]["kimi"]["checks"] == ["version", "tree", "source", "enabled", "smoke"]


def test_r12_kimi_api_outage_is_undetermined_not_absent(monkeypatch):
    def outage(*args):
        raise dv.ka.ApiSchemaError("schema drift")

    monkeypatch.setattr(dv, "_kimi_records", outage)
    assert dv.installed("kimi")["state"] == "undetermined"


def test_r12_live_plugin_envelope_has_no_managed_root(tmp_path, monkeypatch):
    home = tmp_path / ".kimi-code"
    managed = home / "plugins/managed/gates"
    (managed / ".kimi-plugin").mkdir(parents=True)
    (managed / ".kimi-plugin/plugin.json").write_text('{"version":"1.2.3"}')
    record = {"id": "gates", "version": "1.2.3", "enabled": True, "state": "ok",
              "source": "zip-url", "originalSource": gr.release_identity("1.2.3")["url"],
              "commandCount": 5, "hookCount": 3}
    monkeypatch.setattr(dv, "_kimi_home", lambda: home)
    monkeypatch.setattr(dv, "_kimi_call", lambda *args: {"plugins": [record]})
    info = dv.installed("kimi")
    assert info["state"] == "installed"
    assert info["path"] == str(managed) and info["source"] == record["originalSource"]
    record["root"] = str(managed / ".." / "gates")
    assert dv.installed("kimi")["state"] == "installed"
    record["root"] = "relative/gates"
    assert dv.installed("kimi")["state"] == "undetermined"
    record["root"] = str(home / "plugins/managed/other")
    assert dv.installed("kimi")["state"] == "undetermined"


def test_r13_managed_symlink_and_extra_file_are_structural_failures(tmp_path):
    root = tmp_path / "managed"
    root.mkdir()
    (root / "gate.py").write_text("ok")
    tree = _tree(root)
    (root / "extra.py").write_text("shadow")
    assert any("extra.py" in problem for problem in dv.check_tree(root, tree))
    (root / "extra.py").unlink()
    (root / "gate.py").unlink()
    (root / "gate.py").symlink_to(tmp_path / "outside")
    assert dv.check_tree(root, tree)


def test_r37_post_smoke_tree_drift_is_caught(install, monkeypatch):
    monkeypatch.setattr(dv, "installed", lambda _ch: {
        "state": "installed", "version": "1.0.0", "path": str(install)})

    def mutate(_root):
        (install / "scripts" / "post-smoke.py").write_text("shadow")
        return None

    monkeypatch.setattr(dv, "SMOKES", (mutate,))
    spec = json.dumps({"channels": {"claude": {
        "version": "1.0.0", "tree": _tree(install), "checks": ["version", "tree", "smoke"]}}})
    report = dv.cmd_verify(spec)
    assert any("post-smoke.py" in problem["text"] for problem in report["problems"])


def test_r14_kimi_install_uses_canonical_url_then_enables(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(dv, "_kimi_records", lambda: [])
    monkeypatch.setattr(dv, "_kimi_call", lambda method, path, body=None:
                        calls.append((method, path, body)) or {})
    monkeypatch.setattr(dv, "_extract_release", lambda *args: tmp_path)
    monkeypatch.setattr(dv, "check_tree", lambda *args: [])
    source = gr.release_identity("1.2.3")["url"]
    out = dv.cmd_update(json.dumps({"version": "1.2.3", "source": source,
                                    "tree": {"x": "100644 " + "0" * 40},
                                    "zip_sha256": "d" * 64}))
    assert out["ok"]
    assert ("POST", "/api/v1/plugins", {"source": source}) in calls
    assert not any(isinstance(body, dict) and Path(str(body.get("source", ""))).is_absolute()
                   for _m, _p, body in calls)


def test_r48_update_checks_digest_and_tree_before_api_install(tmp_path, monkeypatch):
    source = gr.release_identity("1.2.3")["url"]
    tree = {"scripts/gate.py": "100755 " + "a" * 40}
    spec = json.dumps({"version": "1.2.3", "source": source, "tree": tree,
                       "zip_sha256": "d" * 64})
    events = []
    monkeypatch.setattr(dv, "_extract_release", lambda *args:
                        events.append(("extract", args[-1])) or tmp_path)
    monkeypatch.setattr(dv, "check_tree", lambda *args: [])
    monkeypatch.setattr(dv, "_kimi_records", lambda: [])
    monkeypatch.setattr(dv, "_kimi_call", lambda *args:
                        events.append(("api", args[1])) or {})
    out = dv.cmd_update(spec)
    assert out["ok"] and events[0] == ("extract", "d" * 64)
    assert ("api", "/api/v1/plugins") in events

    events.clear()
    monkeypatch.setattr(dv, "check_tree", lambda *args: ["wrong tree"])
    out = dv.cmd_update(spec)
    assert not out["ok"]
    assert events == [("extract", "d" * 64)]
    assert out["problems"][0]["code"] == "tree"


@pytest.mark.parametrize("state", ("absent", "installed"))
def test_r47_restore_kimi_home_must_match_process_home(state, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(dv, "_kimi_home", lambda: tmp_path / "real-home")
    monkeypatch.setattr(dv, "_kimi_call", lambda *args: calls.append(args))
    monkeypatch.setattr(dv, "_extract_release", lambda *args:
                        pytest.fail("download выполнен до проверки Kimi home"))
    snap = {"state": state, "kimi_home": str(tmp_path / "other-home"),
            "version": "1.0.0", "source": gr.release_identity("1.0.0")["url"],
            "enabled": True, "zip_sha256": "d" * 64}
    out = dv.cmd_restore(json.dumps({"channel": "kimi", "snapshot": snap}))
    assert not out["ok"] and calls == []


def test_r15_claude_directory_marketplace_is_undetermined(monkeypatch, tmp_path):
    home = tmp_path / "home"
    cache = home / ".claude/plugins/cache/lenar-gates/gates/1.0.0/scripts"
    cache.mkdir(parents=True)
    (cache / "ladder_gate.py").write_text("")
    (home / ".claude/plugins").mkdir(parents=True, exist_ok=True)
    (home / ".claude/plugins/installed_plugins.json").write_text(json.dumps({"plugins": {
        "gates@lenar-gates": [{"installPath": str(cache.parent), "version": "1.0.0"}]}}))
    (home / ".claude/plugins/known_marketplaces.json").write_text(json.dumps({"lenar-gates": {
        "source": {"source": "directory", "path": "/tmp/local"}}}))
    monkeypatch.setenv("HOME", str(home))
    info = dv.installed("claude")
    assert info["state"] == "undetermined" and "GitHub" in info["why"]
    (home / ".claude/plugins/known_marketplaces.json").unlink()
    info = dv.installed("claude")
    assert info["state"] == "undetermined" and "marketplace" in info["why"]


def test_r17_r18_cli_updated_but_old_tree_is_caught(harness):
    drafts = []
    harness.setattr(dg.gr, "mark_release_draft", lambda artifact: drafts.append(artifact))
    harness.setattr(dg, "read_inventory", lambda: [{"id": "canary", "channels": ["claude", "kimi"]}])
    harness.setattr(dg, "verify", lambda *args: {"ok": False, "channels": {}, "problems": [
        {"channel": "kimi", "code": "tree", "text": "old tree"}]})
    assert dg.main([]) == 2
    assert len(drafts) == 1


def test_r20_transport_outage_after_mutation_keeps_prerelease(harness):
    drafts = []
    harness.setattr(dg.gr, "mark_release_draft", lambda artifact: drafts.append(artifact))
    harness.setattr(dg, "update", lambda *args: "ssh timeout")
    assert dg.main([]) == 2
    assert drafts == []


@pytest.mark.parametrize(("error", "code"), [
    (dv.ReleaseDigestError("wrong digest"), "zip-sha256"),
    (dv.ReleaseArchiveError("unsafe ZIP"), "source-archive"),
    (dv.InstallError("download failed"), "kimi-install"),
])
def test_r49_update_preserves_artifact_vs_transport_codes(error, code, monkeypatch):
    monkeypatch.setattr(dv, "_extract_release", lambda *args: (_ for _ in ()).throw(error))
    monkeypatch.setattr(dv, "_kimi_call", lambda *args:
                        pytest.fail("API install выполнен после отклонения release"))
    out = dv.cmd_update(json.dumps({
        "version": "9.9.9", "source": gr.release_identity("9.9.9")["url"],
        "tree": {"scripts/gate.py": "100755 " + "a" * 40}, "zip_sha256": "d" * 64}))
    assert not out["ok"]
    assert [problem["code"] for problem in out["problems"]] == [code]


def test_r49_artifact_failure_during_update_raises_artifact_abort(monkeypatch):
    monkeypatch.setattr(dg, "agent", lambda *args: {"ok": False, "problems": [
        {"channel": "kimi", "code": "zip-sha256", "text": "wrong digest"}]})
    with pytest.raises(dg.DeployAbort) as raised:
        dg.update("canary", ["kimi"], versions={"kimi": "9.9.9"},
                  tree={"scripts/gate.py": "100755 " + "a" * 40}, zip_sha256="d" * 64)
    assert raised.value.artifact is True


def test_r22_rollback_requires_tree_enabled_and_smoke(monkeypatch):
    specs = []
    monkeypatch.setattr(dg, "agent", lambda h, cmd, arg:
                        specs.append((cmd, json.loads(arg))) or {"ok": True, "problems": []})
    snap = {"channels": {"kimi": {
        "state": "installed", "version": "1.0.0", "source": gr.release_identity("1.0.0")["url"],
        "tree": {"x": "100644 " + "0" * 40}, "enabled": False, "zip_sha256": "d" * 64}}}
    assert dg.rollback("h", snap) == []
    assert [cmd for cmd, _arg in specs] == ["restore", "verify"]
    assert specs[0][1]["channel"] == "kimi"
    verification = next(arg for cmd, arg in specs if cmd == "verify")
    assert verification["channels"]["kimi"]["checks"] == ["version", "tree", "source", "enabled", "smoke"]
    assert verification["channels"]["kimi"]["enabled"] is False
    assert verification["channels"]["kimi"]["zip_sha256"] == "d" * 64


def test_r36_preflight_pins_rollback_zip_digest(monkeypatch):
    report = {"ok": True, "problems": [], "channels": {"kimi": {"zip_sha256": "d" * 64}}}
    monkeypatch.setattr(dg, "agent", lambda *args: report)
    snap = {"channels": {"kimi": {
        "state": "installed", "version": "1.0.0", "source": gr.release_identity("1.0.0")["url"],
        "tree": {"x": "100644 " + "0" * 40}, "enabled": True}}}
    assert dg.check_rollback_target("h", snap) is None
    assert snap["channels"]["kimi"]["zip_sha256"] == "d" * 64
    report["channels"]["kimi"] = {}
    assert "digest" in dg.check_rollback_target("h", snap)


def test_r22_restore_rechecks_snapshot_digest_before_install(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(dv, "_kimi_home", lambda: tmp_path)
    monkeypatch.setattr(dv, "_extract_release", lambda source, version, dest, digest:
                        events.append(("extract", digest)) or dest)
    monkeypatch.setattr(dv, "_install_kimi_release", lambda source, enabled:
                        events.append(("install", enabled)))
    snap = {"state": "installed", "version": "1.0.0", "kimi_home": str(tmp_path),
            "source": gr.release_identity("1.0.0")["url"], "enabled": False,
            "zip_sha256": "d" * 64}
    out = dv.cmd_restore(json.dumps({"channel": "kimi", "snapshot": snap}))
    assert out["ok"] and events == [("extract", "d" * 64), ("install", False)]
    del snap["zip_sha256"]
    out = dv.cmd_restore(json.dumps({"channel": "kimi", "snapshot": snap}))
    assert not out["ok"] and events == [("extract", "d" * 64), ("install", False)]


def test_r26_subset_writes_partial_journal_and_exit_3(harness, monkeypatch, tmp_path):
    monkeypatch.setenv("GATES_HOSTS", "canary")
    assert dg.main([]) == 3
    journal = json.loads((tmp_path / "state" / "pending").read_text())
    assert journal["status"] == "partial-installed"
    assert not (tmp_path / "state" / "stable").exists()


def test_r28_r40_unknown_sessions_exit_4_without_stable(harness, tmp_path):
    harness.setattr(dg, "session_inventory", lambda hosts: {
        "enumeration": "unknown", "hosts": {h["id"]: {"enumeration": "unknown"} for h in hosts}})
    harness.setattr(dg, "activation", lambda *args: {"complete": False, "status": "reload-required"})
    assert dg.main([]) == 4
    journal = json.loads((tmp_path / "state" / "pending").read_text())
    assert journal["status"] == "installed" and journal["activation"] == "pending"
    assert not (tmp_path / "state" / "stable").exists()


def test_r27_new_api_session_does_not_close_old_tui_inventory():
    inventory = {"enumeration": "known", "sessions": [
        {"host": "h", "channel": "kimi", "id": "old-tui", "kind": "interactive"},
        {"host": "h", "channel": "kimi", "id": "new-api", "kind": "api"}]}
    report = dg.activation_status(inventory, {"h": {"sessions": [
        {"id": "new-api", "version": "1.2.3", "smoke": "passed"}]}})
    assert not report["complete"] and "old-tui" in report["pending"]


def test_r42_every_pre_mutation_id_needs_same_session_behaviour_proof():
    inventory = {"enumeration": "known", "sessions": [
        {"host": "h", "channel": "kimi", "id": "one"}, {"host": "h", "channel": "kimi", "id": "two"}]}
    report = dg.activation_status(inventory, {"h": {"sessions": [
        {"id": "one", "version": "1.2.3", "smoke": "passed"},
        {"id": "two", "closed": True}]}})
    assert not report["complete"] and report["pending"] == ["two"]


def test_r30_operator_text_is_not_activation_proof():
    inventory = {"enumeration": "known", "sessions": [{"host": "h", "channel": "kimi", "id": "one"}]}
    report = dg.activation_status(inventory, {"h": {"operator_confirmed": True, "sessions": []}})
    assert not report["complete"]


def test_r41_stable_is_removed_before_first_mutation_and_stays_removed(harness, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "stable").write_text("old")
    observed = []
    harness.setattr(dg, "session_inventory", lambda hosts: {
        "enumeration": "unknown", "hosts": {h["id"]: {} for h in hosts}})
    harness.setattr(dg, "update", lambda h, c, *args:
                    observed.append((state / "stable").exists()) or "stop")
    assert dg.main([]) == 2
    assert observed == [False] and not (state / "stable").exists()


def test_r32_release_and_api_errors_redact_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(gr, "_gh", lambda args: subprocess.CompletedProcess(
        args, 1, "", "Bearer very-secret-token"))
    with pytest.raises(gr.ReleaseError) as error:
        gr.mark_release_latest(_artifact(tmp_path))
    assert "very-secret-token" not in str(error.value)


def test_r36_real_rollback_zip_matches_snapshot_without_archive_as_extra_file(tmp_path, monkeypatch):
    data = _valid_zip()
    extracted = gr.inspect_archive(data, tmp_path / "expected", "9.9.9")
    want = {"version": "9.9.9", "source": gr.release_identity("9.9.9")["url"],
            "tree": _tree(extracted)}
    monkeypatch.setattr(dv.ka.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(data))
    assert dv._verify_kimi_source(want) == []
    want["tree"]["scripts/gate.py"] = "100755 " + "f" * 40
    assert dv._verify_kimi_source(want)


def test_r19_second_host_snapshot_failure_never_mutates_canary(harness):
    touched = []

    def snapshot(host, channels):
        if host == "vps":
            raise dg.DeployAbort("ssh timeout")
        return {"host": host, "ts": "2026-01-01T00:00:00+00:00",
                "channels": {"claude": {"state": "absent"}}}

    harness.setattr(dg, "snapshot", snapshot)
    harness.setattr(dg, "update", lambda *args: touched.append(args[0]) or None)
    assert dg.main([]) == 2
    assert touched == []


def test_r33_github_and_kimi_guards_are_fail_closed_without_explicit_mocks():
    with pytest.raises(AssertionError, match="GitHub CLI"):
        gr._gh(["release", "view", "v0.0.0-nonexistent"])
    with pytest.raises(AssertionError, match="Kimi Server API"):
        dv._kimi_server()


def test_r12_session_id_cannot_inject_an_api_path(monkeypatch):
    for invalid in ("../other", "..", ".", "a/b"):
        monkeypatch.setattr(dv, "_kimi_call", lambda *args: {
            "items": [{"id": invalid}], "has_more": False})
        report = dv.cmd_sessions("kimi")
        assert report["enumeration"] == "unknown" and report["sessions"] == []


def test_r46_fleet_session_cannot_overwrite_real_host(monkeypatch):
    monkeypatch.setattr(dg, "agent", lambda *args: {
        "enumeration": "known", "sessions": [
            {"host": "vps", "channel": "kimi", "id": "safe", "kind": "api"}]})
    inventory = dg.session_inventory([{"id": "canary", "channels": ["kimi"]}])
    assert inventory["sessions"] == [
        {"host": "canary", "channel": "kimi", "id": "safe", "kind": "api"}]


def test_r46_activate_revalidates_safe_session_id(monkeypatch):
    calls = []
    monkeypatch.setattr(dv, "_kimi_call", lambda *args: calls.append(args))
    report = dv.cmd_activate(json.dumps({"sessions": [{"channel": "kimi", "id": ".."}]}))
    assert calls == [] and report["sessions"] == [] and report["errors"]


def test_r40_live_session_items_envelope_is_enumerated(monkeypatch):
    monkeypatch.setattr(dv, "_kimi_call", lambda *args: {
        "items": [{"id": "session_live", "busy": False}], "has_more": False})
    report = dv.cmd_sessions("kimi")
    assert report == {"enumeration": "known", "sessions": [
        {"channel": "kimi", "id": "session_live", "kind": "unknown"}]}


def test_r16_codex_local_or_unknown_marketplace_is_rejected(monkeypatch, tmp_path):
    path = tmp_path / "codex" / "gates"
    (path / ".codex-plugin").mkdir(parents=True)
    (path / ".codex-plugin/plugin.json").write_text('{"version":"1.0.0+codex"}')
    rows = f"gates@lenar-gates  installed  1.0.0+codex  {path}\n"
    monkeypatch.setattr(dv.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, "lenar-gates\n" + rows, ""))

    def git(_cwd, *args):
        output = "/tmp/local" if args[:2] == ("remote", "get-url") else "a" * 40
        if args[:2] == ("rev-parse", "--show-toplevel"):
            output = str(path)
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(dv, "git", git)
    info = dv.installed("codex")
    assert info["state"] == "undetermined" and "GitHub" in info["why"]


def test_r28_activation_proof_must_match_the_deployed_version():
    inventory = {"enumeration": "known", "sessions": [
        {"host": "h", "channel": "kimi", "id": "interactive"}]}
    observations = {"h": {"sessions": [
        {"id": "interactive", "version": "0.11.3", "smoke": "passed"}]}}
    report = dg.activation_status(inventory, observations, expected_version="0.12.0")
    assert not report["complete"] and report["pending"] == ["interactive"]


def test_r28_activate_invokes_documented_same_session_reload(monkeypatch):
    calls = []

    def api(method, path, body=None):
        calls.append((method, path))
        if method == "GET":
            return {"gates_version": "0.12.0", "gates_smoke": "passed"}
        return {"reloaded": True}

    monkeypatch.setattr(dv, "_kimi_call", api)
    report = dv.cmd_activate(json.dumps({"sessions": [{"channel": "kimi", "id": "live"}]}))
    assert calls == [("POST", "/api/v1/sessions/live:reload"),
                     ("GET", "/api/v1/sessions/live")]
    assert report["sessions"] == [{"id": "live", "version": "0.12.0", "smoke": "passed"}]


def test_r38_draft_retry_uses_authenticated_asset_download(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path)
    extracted = gr.inspect_archive(artifact["zip_bytes"], tmp_path / "expected", "9.9.9")
    artifact["tree"] = _tree(extracted)
    gh = FakeGh({"tagName": "v9.9.9", "isDraft": True, "isPrerelease": False, "assets": [
        {"name": "gates-9.9.9.zip"}, {"name": "gates-9.9.9.zip.sha256"}]})
    original = gh.__call__

    def run(args):
        if args[:2] == ["release", "download"]:
            directory = Path(args[args.index("--dir") + 1])
            pattern = args[args.index("--pattern") + 1]
            source = Path(artifact["zip"] if pattern == artifact["asset"] else artifact["checksum"])
            (directory / pattern).write_bytes(source.read_bytes())
            return subprocess.CompletedProcess(args, 0, "", "")
        return original(args)

    monkeypatch.setattr(gr, "_download", lambda _url: (_ for _ in ()).throw(
        AssertionError("draft asset requested without authentication")))
    gr.publish_release(artifact, gh=run)
    edits = [call for call in gh.calls if call[:2] == ["release", "edit"]]
    assert len(edits) == 1
    assert {"--draft=false", "--prerelease=true", "--latest=false"} <= set(edits[0])


def test_r38_existing_latest_returns_to_prerelease_before_host_mutation(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path)
    gh = FakeGh({"tagName": "v9.9.9", "isDraft": False, "isPrerelease": False, "assets": [
        {"name": "gates-9.9.9.zip"}, {"name": "gates-9.9.9.zip.sha256"}]})
    monkeypatch.setattr(gr, "download_and_verify", lambda *args, **kwargs: None)
    gr.publish_release(artifact, gh=gh)
    edits = [call for call in gh.calls if call[:2] == ["release", "edit"]]
    assert len(edits) == 1
    assert {"--draft=false", "--prerelease=true", "--latest=false"} <= set(edits[0])


def test_r38_release_state_schema_drift_is_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(gr, "download_and_verify", lambda *args, **kwargs:
                        pytest.fail("schema drift дошёл до download"))
    artifact = _artifact(tmp_path)
    gh = FakeGh({"tagName": "v9.9.9", "assets": []})
    original = gh.__call__

    def run(args):
        if args[:2] == ["release", "view"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(
                {"tagName": "v9.9.9", "isDraft": False, "assets": []}), "")
        return original(args)

    with pytest.raises(gr.ReleaseError, match="isPrerelease"):
        gr.publish_release(artifact, gh=run)


def test_r33_review_evidence_and_baseline_are_isolated_from_production():
    real_state = dg.g._REAL_GATE_STATE_DIR()
    assert real_state is not None
    assert not dg.g.PANEL_EVIDENCE.is_relative_to(real_state)
    assert not dg.g.GATE_BASELINE.is_relative_to(real_state)


def test_r39_panel_evidence_is_required_before_artifact_build(monkeypatch):
    monkeypatch.setattr(dg.g, "_panel_evidence_ok", lambda sha: (False, "missing panel"))
    monkeypatch.setattr(dg.ka, "build_artifact", lambda root:
                        pytest.fail("artifact built without panel evidence"))
    with pytest.raises(SystemExit):
        dg.check_review_evidence(SHA)


def test_r28_closed_status_is_not_same_session_behaviour_proof(monkeypatch):
    monkeypatch.setattr(dv, "_kimi_call", lambda method, path, body=None:
                        {"status": "closed"} if method == "GET" else {"reloaded": True})
    report = dv.cmd_activate(json.dumps({"sessions": [{"channel": "kimi", "id": "old"}]}))
    assert report["sessions"] == []
    inventory = {"enumeration": "known", "sessions": [{"host": "h", "channel": "kimi", "id": "old"}]}
    observations = {"h": {"sessions": [{"id": "old", "closed": True}]}}
    assert not dg.activation_status(inventory, observations, "0.12.0")["complete"]


def test_r32_gh_uses_scrubbed_environment(monkeypatch):
    monkeypatch.setenv("GH_HOST", "attacker.invalid")
    monkeypatch.setenv("GH_CONFIG_DIR", "/tmp/attacker")
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid")
    env = gr._gh_env()
    assert env["HOME"] == os.environ["HOME"]
    assert not {"GH_HOST", "GH_CONFIG_DIR", "HTTPS_PROXY"} & set(env)


def test_r38_download_verification_failure_marks_release_draft(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path)
    gh = FakeGh({"tagName": "v9.9.9", "isDraft": False, "isPrerelease": True, "assets": [
        {"name": "gates-9.9.9.zip"}, {"name": "gates-9.9.9.zip.sha256"}]})
    monkeypatch.setattr(gr, "download_and_verify", lambda *args, **kwargs:
                        (_ for _ in ()).throw(gr.ReleaseError("digest mismatch")))
    with pytest.raises(gr.ReleaseError, match="digest mismatch"):
        gr.publish_release(artifact, gh=gh)
    assert any(call[:2] == ["release", "edit"] and "--draft=true" in call for call in gh.calls)


def test_r38_public_download_outage_keeps_prerelease(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path)
    gh = FakeGh({"tagName": "v9.9.9", "isDraft": False, "isPrerelease": True, "assets": [
        {"name": "gates-9.9.9.zip"}, {"name": "gates-9.9.9.zip.sha256"}]})
    monkeypatch.setattr(gr.urllib.request, "urlopen", lambda *args, **kwargs:
                        (_ for _ in ()).throw(OSError("network down")))
    with pytest.raises(gr.ReleaseTransportError, match="network down"):
        gr.publish_release(artifact, gh=gh)
    assert not any(call[:2] == ["release", "edit"] for call in gh.calls)


def test_r38_draft_download_outage_does_not_force_another_state_change(tmp_path):
    artifact = _artifact(tmp_path)
    gh = FakeGh({"tagName": "v9.9.9", "isDraft": True, "isPrerelease": False, "assets": [
        {"name": "gates-9.9.9.zip"}, {"name": "gates-9.9.9.zip.sha256"}]})
    original = gh.__call__

    def run(args):
        if args[:2] == ["release", "download"]:
            return subprocess.CompletedProcess(args, 1, "", "network down")
        return original(args)

    with pytest.raises(gr.ReleaseTransportError, match="network down"):
        gr.publish_release(artifact, gh=run)
    assert not any(call[:2] == ["release", "edit"] for call in gh.calls)


def test_r22_disabled_rollback_does_not_require_enabled_registration(tmp_path, monkeypatch):
    monkeypatch.setattr(dv, "installed", lambda _ch: {
        "state": "installed", "version": "1.0.0", "path": str(tmp_path),
        "enabled": False, "commandCount": 0, "hookCount": 0})
    report = dv.cmd_verify(json.dumps({"channels": {"kimi": {
        "version": "1.0.0", "enabled": False, "checks": ["enabled"]}}}))
    assert report["ok"] and report["problems"] == []


def test_r35_download_requires_exact_canonical_url_and_caps_body(monkeypatch):
    monkeypatch.setattr(gr.urllib.request, "urlopen", lambda *args, **kwargs:
                        pytest.fail("non-canonical URL reached network"))
    canonical = gr.release_identity("1.2.3")["url"]
    for source in (canonical + "?raw=1", canonical + "#x", canonical + "/extra", canonical + ".zip"):
        with pytest.raises(gr.ReleaseError):
            gr._download(source)
    monkeypatch.setattr(gr, "_MAX_DOWNLOAD_BYTES", 100, raising=False)
    monkeypatch.setattr(gr.urllib.request, "urlopen", lambda *args, **kwargs:
                        io.BytesIO(b"x" * 101))
    with pytest.raises(gr.ReleaseError, match="превышает"):
        gr._download(canonical)


def test_r40_paginated_session_inventory_is_unknown(monkeypatch):
    monkeypatch.setattr(dv, "_kimi_call", lambda *args: {
        "items": [{"id": "first"}], "has_more": True})
    assert dv.cmd_sessions("kimi")["enumeration"] == "unknown"


def test_r3_invalid_commit_timestamp_is_rejected(monkeypatch):
    monkeypatch.setattr(gr.g, "_trusted_git_bytes", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args, 0, b"not-a-time\n", b""))
    with pytest.raises(gr.ReleaseError, match="timestamp"):
        gr._archive(SHA)
