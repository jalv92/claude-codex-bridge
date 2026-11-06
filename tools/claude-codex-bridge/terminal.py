"""Open exactly one visible WSL peer window; executable arguments remain data."""
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from contracts import BridgeError, ID, validate
from runtime import atomic, lock, now, process

BOOTSTRAP = "__import__('os').execv('/usr/bin/python3',__import__('json').loads(__import__('base64').b64decode(__import__('sys').argv[1])))"


def configure(bridge, executable=None, distribution=None):
    wt = executable or shutil.which("wt.exe")
    distro = distribution or os.environ.get("WSL_DISTRO_NAME")
    if not wt or not Path(wt).is_file() or not distro or not re.fullmatch(r"[A-Za-z0-9_.-]+", distro):
        raise BridgeError("Automatic windows require Windows Terminal in WSL. Supply --wt and --distribution, or use --manual-peer")
    config = {"kind": "windows-terminal", "executable": str(Path(wt).absolute()),
              "distribution": distro, "wsl_executable": "C:\\Windows\\System32\\wsl.exe"}
    with lock(bridge.root / "config.lock"):
        c = bridge.config()
        c["terminal"] = config
        atomic(bridge.config_file, c)
    return config


def command(config, child_arguments):
    if config.get("kind") != "windows-terminal":
        raise BridgeError("No supported automatic terminal is configured; run configure-terminal")
    # WT treats semicolons as command separators, even for some quoted arguments.
    # Encode the complete Linux argv, including paths, rather than quoting user data
    # through WT/Windows/WSL parsers. No prompt or manifest text is executable code.
    payload = base64.b64encode(json.dumps(child_arguments).encode()).decode()
    return [config["executable"], "-w", "new", "new-tab", "--title", "Codex task review",
            "--suppressApplicationTitle", config["wsl_executable"], "--distribution",
            config["distribution"], "--exec", "/usr/bin/python3", "-c", BOOTSTRAP, payload]


def spawn(config, arguments, log):
    env = {k: os.environ[k] for k in ("HOME", "USER", "PATH", "WSL_INTEROP", "WSL_DISTRO_NAME") if k in os.environ}
    with open(log, "ab", buffering=0) as out:
        child = subprocess.Popen(command(config, arguments), stdin=subprocess.DEVNULL,
            stdout=out, stderr=out, env=env, close_fds=True, start_new_session=True)
    identity = process(child.pid)
    try:
        code = child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        code = None  # A launcher process is not proof of a connected peer.
    if code not in (0, None):
        raise BridgeError(f"Windows Terminal launch failed ({code}); see {log}")
    return {"launcher": identity, "launcher_exit_code": code, "requested_at": now()}


def open_peer(loops, identifier, launcher=spawn):
    from bridge import SOURCE
    validate(identifier, ID)
    directory = loops.directory(identifier)
    with lock(directory / "terminal.lock"):
        s = loops.read(identifier)
        if s.get("terminal_launch") or s.get("peer_phase"):
            return s.get("terminal_launch", {"status": "PEER_ALREADY_CONNECTED"})
        if s["state"] not in ("READY", "QUEUED") or time.time() >= s["expires_epoch"]:
            raise BridgeError("Only a published, unexpired task can open its Codex window")
        terminal = loops.bridge.config().get("terminal")
        if not terminal:
            raise BridgeError("Automatic terminal is not configured. Run configure-terminal once")
        with lock(directory / "lock"):
            s = loops.read(identifier)
            loops.save(directory, s, terminal_launch={"status": "REQUESTED", "at": now()})
        try:
            launched = launcher(terminal, ["/usr/bin/python3", str(SOURCE / "bridge.py"),
                "--runtime", str(loops.bridge.root), "loop-peer", identifier, "--hold"], directory / "terminal.log")
        except Exception as exc:
            with lock(directory / "lock"):
                s = loops.read(identifier)
                loops.save(directory, s, terminal_launch={"status": "FAILED", "error": str(exc)})
            raise
        with lock(directory / "lock"):
            s = loops.read(identifier)
            loops.save(directory, s, terminal_launch={"status": "REQUESTED", **launched})
        # The coordinator acknowledges from inside the actual newly opened window.
        deadline = min(time.time() + 20, s["expires_epoch"])
        while time.time() < deadline:
            s = loops.read(identifier)
            if s.get("peer_phase"):
                with lock(directory / "lock"):
                    s = loops.read(identifier)
                    loops.save(directory, s, terminal_launch={"status": "CONNECTED", **launched})
                return s["terminal_launch"]
            time.sleep(.1)
        raise BridgeError("Windows Terminal was requested but its peer did not connect. Check terminal.log; do not silently open duplicates")
