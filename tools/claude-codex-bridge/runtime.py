"""Atomic persistence, Linux process identities, and isolated Git snapshots."""
import contextlib
import fcntl
import json
import os
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from contracts import BridgeError


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data if isinstance(data, bytes) else (json.dumps(data, indent=2) + "\n").encode()
    fd, name = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def lock(path, blocking=True):
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def process(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        fields = text[text.rfind(")") + 2:].split()
        if fields[0] == "Z":
            return None
        return {"pid": int(pid), "start": int(fields[19]),
                "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "pid_namespace": os.readlink(f"/proc/{pid}/ns/pid"),
                "ppid": int(fields[1]), "group": int(fields[2]), "session": int(fields[3])}
    except (OSError, ValueError, IndexError):
        return None


def alive(identity):
    p = process(identity["pid"]) if identity else None
    return bool(p and all(p[k] == identity[k] for k in ("pid", "start", "boot"))
                and p["pid_namespace"] == identity.get("pid_namespace", p["pid_namespace"]))


def descendants(pid):
    allp = [p for f in Path("/proc").iterdir() if f.name.isdigit()
            if (p := process(int(f.name)))]
    ids = {pid}
    for _ in range(64):
        expanded = ids | {p["pid"] for p in allp if p["ppid"] in ids}
        if expanded == ids:
            break
        ids = expanded
    return [p for p in allp if p["pid"] in ids and p["pid"] != pid]


def terminate_owned(identities):
    """Signal only birth-checked job processes, including separate child sessions."""
    known = {p["pid"]: p for p in identities if p}
    for p in list(known.values()):
        if alive(p):
            known.update({c["pid"]: c for c in descendants(p["pid"])})
    # Freeze first, preventing live parents from spawning between enumeration and kill.
    for p in known.values():
        if alive(p):
            try:
                os.kill(p["pid"], signal.SIGSTOP)
            except ProcessLookupError:
                pass
    for p in list(known.values()):
        if alive(p):
            known.update({c["pid"]: c for c in descendants(p["pid"])})
    for p in reversed(list(known.values())):
        if alive(p):
            try:
                os.kill(p["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass


def claude_parent():
    pid = os.getppid()
    for _ in range(64):
        p = process(pid)
        if not p:
            return None
        try:
            name = Path(os.readlink(f"/proc/{pid}/exe")).name
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[:2]
            if name in ("claude", "claude.exe") or any(
                    b"@anthropic-ai/claude-code" in a for a in argv):
                return p
        except OSError:
            pass
        pid = p["ppid"]
    return None


def git(project, *args, timeout=30, binary=False):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_OPTIONAL_LOCKS": "0"}
    r = subprocess.run(["/usr/bin/git", "-C", str(project), *args],
                       capture_output=True, timeout=timeout, env=env)
    if r.returncode:
        raise BridgeError("Git failed: " + r.stderr.decode(errors="replace").strip()[:1000])
    return r.stdout if binary else r.stdout.decode().strip()


def snapshot(project, commit, target):
    """Copy exact Git blobs. Never checkout/filter/hooks, export-ignore, or source writes."""
    target.mkdir()
    entries = git(project, "ls-tree", "-rz", "--full-tree", commit, binary=True).split(b"\0")
    for entry in entries:
        if not entry:
            continue
        info, rawname = entry.split(b"\t", 1)
        mode, kind, oid = info.decode().split()
        rel = Path(os.fsdecode(rawname))
        if rel.is_absolute() or ".." in rel.parts or ".git" in rel.parts:
            raise BridgeError("Unsafe Git tree path")
        if kind != "blob":
            raise BridgeError(f"Submodule not included in snapshot: {rel}; supply explicit artifacts")
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        blob = git(project, "cat-file", "blob", oid, binary=True)
        if mode == "120000":
            link = os.fsdecode(blob)
            if not (dest.parent / link).resolve().is_relative_to(target.resolve()):
                raise BridgeError(f"External symlink not allowed in snapshot: {rel}")
            dest.symlink_to(link)
        else:
            dest.write_bytes(blob)
            dest.chmod(0o555 if mode == "100755" else 0o444)
    for directory in sorted((p for p in target.rglob("*") if p.is_dir() and not p.is_symlink()),
                            key=lambda p: len(p.parts), reverse=True):
        directory.chmod(0o555)
    target.chmod(0o555)
