#!/usr/bin/env python3
import hashlib
import json
import os
import pwd
import stat
import subprocess
import sys
from pathlib import Path

BOOT_SHA256 = "a669a28804370f4134ea2cef0ea598c8e323c549076643ffecb5ebc8675973ae"
SCRIPTS = frozenset({"codex_review_gate.py", "ladder_gate.py", "prepush_gate.py"})


def _home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _git_binary() -> "str | None":
    for candidate in ("/usr/bin/git", "/usr/local/bin/git", "/opt/homebrew/bin/git"):
        try:
            info = os.lstat(candidate)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) \
                and info.st_uid == 0 and not info.st_mode & 0o022 and os.access(candidate, os.X_OK):
            return candidate
    return None


def _onboarded(payload: dict) -> bool:
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return False
    git_seen = False
    for candidate in (Path(cwd), *Path(cwd).parents):
        git_seen = git_seen or os.path.lexists(candidate / ".git")
    binary = _git_binary()
    if binary is None:
        if git_seen:
            raise RuntimeError("репозиторий с конфигом гейта существует, но trusted git недоступен")
        return False
    env = {"PATH": "/usr/bin:/bin", "HOME": str(_home()), "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1"}
    root = subprocess.run([binary, "-C", cwd, "rev-parse", "--show-toplevel"], env=env,
                          capture_output=True, text=True)
    if root.returncode:
        if git_seen:
            raise RuntimeError("репозиторий с конфигом гейта существует, но rev-parse не выполнен")
        return False
    repo = Path(root.stdout.strip())
    if os.path.lexists(repo / ".codex-gate.yaml"):
        return True
    committed = subprocess.run([binary, "-C", str(repo), "ls-tree", "HEAD", "--",
                                ".codex-gate.yaml"], env=env, capture_output=True, text=True)
    if committed.returncode:
        refs = subprocess.run([binary, "-C", str(repo), "show-ref", "--head"], env=env,
                              capture_output=True, text=True)
        if refs.returncode in (0, 1) and not refs.stdout.strip() and not refs.stderr.strip():
            return False
        raise RuntimeError("не удалось проверить .codex-gate.yaml в HEAD")
    return bool(committed.stdout.strip())


def main(argv) -> int:
    if len(argv) < 2 or argv[0] not in SCRIPTS:
        return 2
    try:
        payload = json.load(sys.stdin)
    except (ValueError, UnicodeError):
        return 2
    if not isinstance(payload, dict):
        return 2
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return 2
    if not _onboarded(payload):
        return 0
    home = _home()
    for directory in (home, home / ".local", home / ".local/lib",
                      home / ".local/lib/claude-gates"):
        info = directory.lstat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022 \
                or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            return 2
    resolver = home / ".local/lib/claude-gates/kimi_install.py"
    info = resolver.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) \
            or info.st_uid != os.getuid() or info.st_mode & 0o022:
        return 2
    data = resolver.read_bytes()
    if hashlib.sha256(data).hexdigest() != BOOT_SHA256:
        return 2
    namespace = {"__name__": "kimi_install", "__file__": str(resolver)}
    exec(compile(data, str(resolver), "exec"), namespace)
    selected = namespace["resolve_kimi"](argv[0])
    if selected is None:
        return 2
    os.execv(sys.executable, [sys.executable, "-I", "-B", str(selected), *argv[1:]])


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:
        print(f"[gates] native hook rejected: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
