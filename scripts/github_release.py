#!/usr/bin/env python3
"""Immutable GitHub Release artifacts for the gates plugin."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins" / "gates" / "scripts"))
import codex_review_gate as g  # noqa: E402
from deploy_verify import tree_digest  # noqa: E402


REPOSITORY = "shagiev/claude-gates"
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z", re.ASCII)
_REQUIRED = (".kimi-plugin/plugin.json", ".claude-plugin/plugin.json")
_MAX_ENTRY_BYTES = 16 * 1024 * 1024
_MAX_EXTRACT_BYTES = 100 * 1024 * 1024


class ReleaseError(RuntimeError):
    pass


class ReleaseTransportError(ReleaseError):
    pass


def release_identity(version: str) -> dict:
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise ReleaseError(f"недопустимая release-версия: {version!r}")
    asset = f"gates-{version}.zip"
    return {
        "version": version,
        "tag": f"v{version}",
        "asset": asset,
        "url": f"https://github.com/{REPOSITORY}/releases/download/v{version}/{asset}",
        "checksum_asset": asset + ".sha256",
    }


def _safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name and "\\" not in name and not path.is_absolute()
                and ".." not in path.parts and not name.startswith("plugins/gates/")
                and "__pycache__" not in path.parts and not name.endswith(".pyc"))


def inspect_archive(data: bytes, destination: Path, version: "str | None" = None) -> Path:
    """Validate and extract a plugin-root ZIP without trusting archive paths or file types."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with zipfile.ZipFile(__import__("io").BytesIO(data)) as bundle:
            names = [info.filename for info in bundle.infolist() if not info.is_dir()]
            if len(names) != len(set(names)):
                raise ReleaseError("ZIP содержит повторяющиеся имена")
            for info in bundle.infolist():
                name = info.filename
                if not _safe_name(name):
                    raise ReleaseError(f"небезопасный путь в ZIP: {name!r}")
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if info.is_dir():
                    continue
                if kind not in (0, stat.S_IFREG) or info.flag_bits & 1:
                    raise ReleaseError(f"необычный или зашифрованный entry в ZIP: {name!r}")
                if info.file_size > _MAX_ENTRY_BYTES:
                    raise ReleaseError(f"ZIP entry превышает per-entry limit: {name!r}")
                if total + info.file_size > _MAX_EXTRACT_BYTES:
                    raise ReleaseError("ZIP total uncompressed size превышает limit")
                target = destination.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with bundle.open(info) as source, open(target, "wb") as output:
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        total += len(chunk)
                        if written > _MAX_ENTRY_BYTES:
                            raise ReleaseError(f"ZIP entry превышает per-entry limit: {name!r}")
                        if total > _MAX_EXTRACT_BYTES:
                            raise ReleaseError("ZIP total uncompressed size превышает limit")
                        output.write(chunk)
                if written != info.file_size:
                    raise ReleaseError(f"ZIP entry size drift: {name!r}")
                target.chmod(0o755 if mode & 0o100 else 0o644)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise ReleaseError(f"ZIP не прочитан: {type(exc).__name__}: {exc}") from exc
    for name in _REQUIRED:
        if not (destination / name).is_file():
            raise ReleaseError(f"ZIP не содержит {name} в корне плагина")
    try:
        for name in _REQUIRED:
            manifest = json.loads((destination / name).read_text())
            if version is not None and manifest.get("version") != version:
                raise ReleaseError(f"{name}: версия {manifest.get('version')!r} != {version!r}")
    except (OSError, ValueError, AttributeError) as exc:
        raise ReleaseError(f"manifest в ZIP не прочитан: {exc}") from exc
    return destination


def _blob_entry(path: Path):
    data = path.read_bytes()
    mode = "100755" if path.stat().st_mode & 0o100 else "100644"
    oid = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    return mode, oid


def expected_disk_tree(root: Path) -> dict:
    root = Path(root)
    if root.is_symlink():
        raise ReleaseError("корень извлечённого артефакта — symlink")
    tree = {}
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            if (Path(base) / name).is_symlink():
                raise ReleaseError(f"symlink в артефакте: {Path(base) / name}")
        for name in files:
            path = Path(base) / name
            if path.is_symlink() or not path.is_file():
                raise ReleaseError(f"необычный файл в артефакте: {path}")
            tree[path.relative_to(root).as_posix()] = " ".join(_blob_entry(path))
    return tree


def _archive(sha: str) -> bytes:
    timestamp = g._trusted_git_bytes("show", "-s", "--format=%ct", sha, cwd=ROOT)
    if timestamp is None or timestamp.returncode != 0 \
            or re.fullmatch(rb"[0-9]+", timestamp.stdout.strip(), re.ASCII) is None:
        raise ReleaseError("commit timestamp для воспроизводимого ZIP не прочитан")
    mtime = "@" + timestamp.stdout.strip().decode("ascii")
    result = g._trusted_git_bytes("archive", "--format=zip", f"--mtime={mtime}",
                                  f"{sha}:plugins/gates", cwd=ROOT)
    if result is None or result.returncode != 0 or not result.stdout:
        detail = (result.stderr or b"") if result is not None else b"trusted git unavailable"
        raise ReleaseError("git archive не выполнен: " + detail.decode("utf-8", "replace")[-300:])
    return result.stdout


def build_release_artifact(sha: str, version: str, expected_tree: dict, directory: Path) -> dict:
    identity = release_identity(version)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    first = _archive(sha)
    second = _archive(sha)
    if first != second:
        raise ReleaseError("повторная сборка ZIP дала другие байты")
    extracted = inspect_archive(first, directory / "verify", version)
    actual_tree = expected_disk_tree(extracted)
    if actual_tree != expected_tree:
        raise ReleaseError("дерево ZIP не совпадает с plugins/gates закреплённого коммита")
    archive = directory / identity["asset"]
    archive.write_bytes(first)
    digest = hashlib.sha256(first).hexdigest()
    checksum = directory / identity["checksum_asset"]
    checksum.write_text(f"{digest}  {identity['asset']}\n")
    return {**identity, "sha": sha, "zip": str(archive), "checksum": str(checksum),
            "zip_sha256": digest, "tree": actual_tree, "digest": tree_digest(actual_tree)}


def _gh_env():
    return {name: os.environ[name] for name in
            ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT")
            if name in os.environ}


def _gh(args):
    binary = next((path for path in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh")
                   if Path(path).is_file() and os.access(path, os.X_OK)), None)
    if binary is None:
        raise ReleaseError("GitHub CLI не найден в trusted system paths")
    return subprocess.run([binary, *args], capture_output=True, text=True, env=_gh_env())


def _clean_error(result) -> str:
    text = ((result.stderr or result.stdout or "") if result is not None else "нет ответа")
    text = re.sub(r"(?i)(bearer\s+|token[=:\s]+)[A-Za-z0-9._~+/=-]+", r"\1<redacted>", text)
    return g.redact_secrets(g.strip_ansi(text)).strip()[-400:]


def _run_gh(gh, args, ok=(0,)):
    result = gh(args)
    if result.returncode not in ok:
        raise ReleaseError(f"gh {' '.join(args[:3])}: rc={result.returncode}: {_clean_error(result)}")
    return result


_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
_DOWNLOAD_URL = re.compile(
    rf"https://github\.com/{REPOSITORY}/releases/download/"
    r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)/"
    r"gates-\1\.\2\.\3\.zip(?:\.sha256)?", re.ASCII)


def _download(url: str) -> bytes:
    if _DOWNLOAD_URL.fullmatch(url) is None:
        raise ReleaseError("download URL не является каноническим GitHub Release URL")
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read(_MAX_DOWNLOAD_BYTES + 1)
    except OSError as exc:
        raise ReleaseTransportError(f"Release asset не скачан: {type(exc).__name__}: {exc}") from exc
    if len(data) > _MAX_DOWNLOAD_BYTES:
        raise ReleaseError("Release asset превышает 100 MiB")
    return data


def download_and_verify(artifact: dict, gh=None, draft=False) -> None:
    if draft:
        with tempfile.TemporaryDirectory(prefix="gates-release-draft-") as tmp:
            for name in (artifact["asset"], artifact["checksum_asset"]):
                try:
                    _run_gh(gh or _gh, ["release", "download", artifact["tag"], "--repo", REPOSITORY,
                                        "--pattern", name, "--dir", tmp])
                except ReleaseError as exc:
                    raise ReleaseTransportError(str(exc)) from exc
            data = (Path(tmp) / artifact["asset"]).read_bytes()
            checksum = (Path(tmp) / artifact["checksum_asset"]).read_text()
    else:
        data = _download(artifact["url"])
    if hashlib.sha256(data).hexdigest() != artifact["zip_sha256"]:
        raise ReleaseError("скачанный ZIP не совпадает с publisher ZIP SHA-256")
    with tempfile.TemporaryDirectory(prefix="gates-release-download-") as tmp:
        root = inspect_archive(data, Path(tmp) / "plugin", artifact["version"])
        if expected_disk_tree(root) != artifact["tree"]:
            raise ReleaseError("дерево скачанного ZIP не совпадает с publisher commit tree")
    if not draft:
        checksum = _download(artifact["url"] + ".sha256").decode("utf-8")
    if checksum.strip() != f"{artifact['zip_sha256']}  {artifact['asset']}":
        raise ReleaseError("скачанный .sha256 не совпадает с publisher digest")


def _require_tag_target(artifact: dict, tag: str, gh) -> None:
    target = _run_gh(gh, ["api", f"repos/{REPOSITORY}/commits/{tag}", "--jq", ".sha"])
    actual_sha = target.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", actual_sha, re.ASCII) is None:
        raise ReleaseError("GitHub tag target не является полным commit SHA")
    if actual_sha != artifact["sha"]:
        raise ReleaseError(f"tag {tag} указывает на {actual_sha}, ожидался {artifact['sha']}")


def publish_release(artifact: dict, gh=None) -> None:
    gh = gh or _gh
    identity = release_identity(artifact["version"])
    tag = identity["tag"]
    view = gh(["release", "view", tag, "--repo", REPOSITORY, "--json",
               "tagName,isDraft,isPrerelease,assets"])
    exists = view.returncode == 0
    if not exists and "release not found" not in (view.stderr or "").lower():
        raise ReleaseError(f"Release {tag} не прочитан: {_clean_error(view)}")
    if exists:
        try:
            release = json.loads(view.stdout)
            if not isinstance(release.get("isDraft"), bool) \
                    or not isinstance(release.get("isPrerelease"), bool):
                raise ReleaseError(f"схема Release {tag}: isDraft/isPrerelease не boolean")
            assets = {entry["name"] for entry in release["assets"]}
        except (ValueError, KeyError, TypeError) as exc:
            raise ReleaseError(f"схема Release {tag} не распознана: {exc}") from exc
        _require_tag_target(artifact, tag, gh)
        unexpected = assets - {identity["asset"], identity["checksum_asset"]}
        if unexpected:
            raise ReleaseError(f"Release содержит конфликтующие assets: {sorted(unexpected)}")
        missing = [path for key, path in (("zip", artifact["zip"]), ("checksum", artifact["checksum"]))
                   if identity["asset" if key == "zip" else "checksum_asset"] not in assets]
        if missing:
            _run_gh(gh, ["release", "upload", tag, *missing, "--repo", REPOSITORY])
    else:
        _run_gh(gh, ["release", "create", tag, artifact["zip"], artifact["checksum"],
                     "--repo", REPOSITORY, "--target", artifact["sha"], "--title", tag,
                     "--notes", f"Gates {artifact['version']} from reviewed commit {artifact['sha']}",
                     "--prerelease", "--latest=false"])
    try:
        if not exists:
            _require_tag_target(artifact, tag, gh)
        download_and_verify(artifact, gh=gh, draft=exists and release.get("isDraft") is True)
    except ReleaseTransportError:
        raise
    except ReleaseError as exc:
        try:
            mark_release_draft(artifact, gh=gh)
        except ReleaseError as draft_exc:
            raise ReleaseError(f"{exc}; release не переведён в draft: {draft_exc}") from exc
        raise
    if exists and (release["isDraft"] or not release["isPrerelease"]):
        _run_gh(gh, ["release", "edit", tag, "--repo", REPOSITORY,
                     "--draft=false", "--prerelease=true", "--latest=false"])


def mark_release_draft(artifact: dict, gh=None) -> None:
    _run_gh(gh or _gh, ["release", "edit", artifact["tag"], "--repo", REPOSITORY,
                        "--draft=true", "--latest=false"])


def mark_release_latest(artifact: dict, gh=None) -> None:
    _run_gh(gh or _gh, ["release", "edit", artifact["tag"], "--repo", REPOSITORY,
                        "--draft=false", "--prerelease=false", "--latest"])
