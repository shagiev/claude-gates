#!/usr/bin/env python3
"""Trusted installation resolver shared by gates-run and Kimi acceptance checks."""
import hashlib
import json
import os
import pwd
import stat
import sys
from pathlib import Path

SCRIPTS = frozenset({"codex_review_gate.py", "ladder_gate.py", "prepush_gate.py"})
KIMI_MANIFESTS = (".kimi-plugin/plugin.json", "kimi.plugin.json")
EXTRA_DIRS = frozenset({".git", ".in_use", "__pycache__"})
EXTRA_FILES = frozenset({".DS_Store", ".orphaned_at", ".codex-marketplace-install.json"})


class InstallError(Exception):
    pass


def _home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _file(path: Path) -> Path:
    try:
        st = path.lstat()
    except OSError as exc:
        raise InstallError(f"не прочитан файл {path}: {exc}") from exc
    if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise InstallError(f"не обычный файл или symlink: {path}")
    if st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise InstallError(f"владелец/права небезопасны: {path}")
    return path


def _dir(path: Path) -> Path:
    try:
        st = path.lstat()
    except OSError as exc:
        raise InstallError(f"не прочитан каталог {path}: {exc}") from exc
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise InstallError(f"не обычный каталог или symlink: {path}")
    if st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise InstallError(f"владелец/права каталога небезопасны: {path}")
    return path


def _json(path: Path):
    _file(path)
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, UnicodeError) as exc:
        raise InstallError(f"JSON не разобран {path}: {exc}") from exc


def _no_bytecode(root: Path) -> None:
    for current, dirs, files in os.walk(root, followlinks=False):
        if "__pycache__" in dirs:
            raise InstallError(f"bytecode cache в установке: {Path(current) / '__pycache__'}")
        for name in files:
            if name.endswith((".pyc", ".pyo")):
                raise InstallError(f"bytecode в установке: {Path(current) / name}")


def _script(root: Path, name: str) -> Path:
    if name not in SCRIPTS:
        raise InstallError(f"script name не из allowlist: {name!r}")
    _no_bytecode(root)
    _dir(root / "scripts")
    for sibling in SCRIPTS:
        _file(root / "scripts" / sibling)
    path = root / "scripts" / name
    _file(path)
    if path.resolve() != (root / "scripts" / name).resolve():
        raise InstallError(f"script вышел из managed root: {path}")
    return path


def _validate_manifest(root: Path) -> None:
    alternate = root / "kimi.plugin.json"
    if alternate.exists() or alternate.is_symlink():
        raise InstallError(f"одновременно два Kimi manifest: {alternate}")
    _dir(root / ".kimi-plugin")
    path = root / ".kimi-plugin/plugin.json"
    data = _json(path)
    if not isinstance(data, dict) or data.get("name") != "gates":
        raise InstallError(f"невалидный Kimi manifest: {path}")
    for key in ("skills", "commands"):
        if data.get(key) != f"./{key}/":
            raise InstallError(f"manifest.{key} должен быть точным ./{key}/")
        target = (root / data[key]).resolve()
        if root.resolve() not in target.parents:
            raise InstallError(f"manifest.{key} выходит из managed root")
        _dir(target)
    commands = root / "commands"
    expected_commands = {"design-review.md", "simplify.md", "security-review.md",
                         "code-review.md", "gates-init.md"}
    actual_commands = {path.name for path in commands.glob("*.md")}
    if actual_commands != expected_commands:
        raise InstallError(f"Kimi commands drift: {sorted(actual_commands)}")
    for name in expected_commands:
        _file(commands / name)
    for skill in (root / "skills").glob("*/SKILL.md"):
        _dir(skill.parent)
        _file(skill)
    hooks = data.get("hooks")
    if not isinstance(hooks, list) or len(hooks) != 3 \
            or not all(isinstance(hook, dict) for hook in hooks):
        raise InstallError("Kimi manifest.hooks не задан")
    edit_matcher = ("^(?:(?:Multi)?Edit.*|Write.*|NotebookEdit.*|apply_patch.*|"
                    "mcp__.*__.*(?:Edit|edit|Write|write|Patch|patch|Replace|replace|Truncate|truncate).*|"
                    "mcp__.*__.*(?:Create|create|Update|update|Save|save|Delete|delete|Remove|remove|Move|move|Rename|rename|Append|append|Copy|copy).*(?:File|file|Path|path|Folder|folder|Directory|directory).*|"
                    "mcp__.*__.*(?:File|file|Path|path|Folder|folder|Directory|directory).*(?:Create|create|Update|update|Save|save|Delete|delete|Remove|remove|Move|move|Rename|rename|Append|append|Copy|copy).*)$")
    native_digest = hashlib.sha256(_file(root / "scripts/native_hook.py").read_bytes()).hexdigest()
    prefix = ("/usr/bin/python3 -I -B -c 'import hashlib,json,os,pwd,sys,subprocess\n"
              "from pathlib import Path\n"
              "try:\n"
              " payload=json.load(sys.stdin)\n"
              " cwd=payload.get(\"cwd\")\n"
              " if not isinstance(cwd,str) or not os.path.isabs(cwd): sys.exit(2)\n"
              " env={\"PATH\":\"/usr/bin:/bin\",\"HOME\":pwd.getpwuid(os.getuid()).pw_dir,\"GIT_CONFIG_GLOBAL\":\"/dev/null\",\"GIT_CONFIG_NOSYSTEM\":\"1\",\"GIT_NO_REPLACE_OBJECTS\":\"1\",\"GIT_TERMINAL_PROMPT\":\"0\"}\n"
              " root=subprocess.run([\"/usr/bin/git\",\"-C\",cwd,\"rev-parse\",\"--show-toplevel\"],env=env,capture_output=True,text=True)\n"
              " if root.returncode:\n"
              "  if any(os.path.lexists(p/\".git\") for p in (Path(cwd),*Path(cwd).parents)): sys.exit(2)\n"
              "  sys.exit(0)\n"
              " repo=Path(root.stdout.strip())\n"
              " if not os.path.lexists(repo/\".codex-gate.yaml\"):\n"
              "  committed=subprocess.run([\"/usr/bin/git\",\"-C\",str(repo),\"ls-tree\",\"HEAD\",\"--\",\".codex-gate.yaml\"],env=env,capture_output=True,text=True)\n"
              "  if committed.returncode==0 and not committed.stdout.strip(): sys.exit(0)\n"
              "  refs=subprocess.run([\"/usr/bin/git\",\"-C\",str(repo),\"show-ref\",\"--head\"],env=env,capture_output=True,text=True)\n"
              "  if refs.returncode in (0,1) and not refs.stdout.strip() and not refs.stderr.strip(): sys.exit(0)\n"
              "  if committed.returncode: sys.exit(2)\n"
              " p=pwd.getpwuid(os.getuid()).pw_dir+\"/.local/lib/claude-gates/native_hook.py\"\n"
              " d=open(p,\"rb\").read()\n"
              " if hashlib.sha256(d).hexdigest()!=\"" + native_digest + "\": sys.exit(2)\n"
              "except Exception:\n"
              " sys.exit(2)\n"
              "sys.argv=[p]+sys.argv[1:]\n"
              "exec(compile(d,p,\"exec\"),{\"__name__\":\"__main__\",\"__file__\":p})' codex_review_gate.py ")
    expected_hooks = {
        ("PreToolUse", edit_matcher, prefix + "gate-edit"),
        ("PreToolUse", "Bash", prefix + "gate-bash"),
        ("SessionStart", "", prefix + "clear-marker"),
    }
    actual_hooks = {(hook.get("event"), hook.get("matcher"), hook.get("command"))
                    for hook in hooks}
    if actual_hooks != expected_hooks:
        raise InstallError("Kimi hook schema drift")


def _secure_dir(home: Path, *parts: str) -> Path:
    current = _dir(home)
    for part in parts:
        current = _dir(current / part)
    return current


def resolve_kimi(name: str, home: "Path | None" = None) -> "Path | None":
    home = home or _home()
    registry = home / ".kimi-code/plugins/installed.json"
    if not registry.exists():
        return None
    _secure_dir(home, ".kimi-code", "plugins")
    data = _json(registry)
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        raise InstallError("Kimi installed.json schema drift")
    records = [r for r in data["plugins"] if isinstance(r, dict) and r.get("id") == "gates"]
    if len(records) > 1:
        raise InstallError("несколько записей gates в Kimi installed.json")
    if not records:
        return None
    rec = records[0]
    if rec.get("enabled") is not True:
        return None
    if "state" in rec and rec["state"] != "ok":
        raise InstallError(f"Kimi plugin не активен: state={rec['state']!r}")
    root_raw = rec.get("root")
    if not isinstance(root_raw, str) or not os.path.isabs(root_raw):
        raise InstallError("Kimi root не абсолютный")
    expected = home / ".kimi-code/plugins/managed/gates"
    root = Path(root_raw)
    if root.resolve() != expected.resolve():
        raise InstallError(f"Kimi root вне managed: {root_raw}")
    _secure_dir(home, ".kimi-code", "plugins", "managed", "gates")
    _validate_manifest(root)
    return _script(root, name)


def _claude_candidate(root: Path, name: str, cache: Path, home: Path) -> "Path | None":
    _secure_dir(home, ".claude", "plugins", "cache", "lenar-gates", "gates")
    if root.resolve() != cache.resolve() and cache.resolve() not in root.resolve().parents:
        raise InstallError(f"Claude installPath вне canonical cache: {root}")
    _dir(root)
    return _script(root, name)


def resolve_claude(name: str, home: "Path | None" = None) -> "Path | None":
    home = home or _home()
    cache = home / ".claude/plugins/cache/lenar-gates/gates"
    registry = home / ".claude/plugins/installed_plugins.json"
    if not registry.exists():
        return None
    _secure_dir(home, ".claude", "plugins")
    data = _json(registry)
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), dict):
        raise InstallError("Claude installed_plugins.json schema drift")
    entries = data["plugins"].get("gates@lenar-gates") or []
    if not isinstance(entries, list):
        raise InstallError("Claude gates entries schema drift")
    for rec in entries:
        if not isinstance(rec, dict):
            raise InstallError("Claude gate entry schema drift")
        raw = rec.get("installPath") or ""
        if not os.path.isabs(raw):
            continue
        root = Path(raw)
        if (root / "scripts" / name).is_file():
            return _claude_candidate(root, name, cache, home)
    return None


def delivery_digest(root: Path) -> str:
    root = Path(root)
    _no_bytecode(root)
    h = hashlib.sha256()
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXTRA_DIRS)
        for d in dirs:
            candidate = Path(current) / d
            if candidate.is_symlink():
                raise InstallError(f"symlink-каталог в поставке: {candidate}")
        for name in sorted(files):
            path = Path(current) / name
            rel = path.relative_to(root).as_posix()
            if name in EXTRA_FILES:
                continue
            if path.is_symlink():
                raise InstallError(f"symlink в поставке: {path}")
            data = path.read_bytes()
            mode = "100755" if path.stat().st_mode & 0o100 else "100644"
            h.update(f"{mode} {hashlib.sha256(data).hexdigest()}\t{rel}\n".encode())
    return h.hexdigest()


def resolve_script(name: str, home: "Path | None" = None) -> "Path | None":
    home = home or _home()
    claude = resolve_claude(name, home)
    kimi = resolve_kimi(name, home)
    if claude and kimi and delivery_digest(claude.parent.parent) != delivery_digest(kimi.parent.parent):
        raise InstallError("Claude и Kimi installations имеют разный delivery digest")
    return claude or kimi


def has_kimi_manifest(tree: dict) -> bool:
    return any(path in tree for path in KIMI_MANIFESTS)


def main(argv):
    if len(argv) != 2 or argv[0] not in ("select", "claude", "kimi", "digest"):
        print("usage: kimi_install.py select|claude|kimi <script.py> | digest <root>",
              file=sys.stderr)
        return 2
    try:
        if argv[0] == "digest":
            print(delivery_digest(Path(argv[1])))
        else:
            resolver = {"select": resolve_script, "claude": resolve_claude,
                        "kimi": resolve_kimi}[argv[0]]
            result = resolver(argv[1])
            if result is not None:
                print(result)
        return 0
    except InstallError as exc:
        print(f"[gates] installation rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
