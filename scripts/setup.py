#!/usr/bin/env python3
"""Install this checkout's bridge hooks and /codex-loop command in one project."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools/claude-codex-bridge"
sys.path.insert(0, str(SOURCE))
from bridge import Bridge, DEFAULT_RUNTIME
from contracts import BridgeError
from runtime import atomic, lock


def render_command(runtime, manual=False):
    text = (ROOT / "templates/codex-loop.md").read_text()
    values = {"BRIDGE_COMMAND": shlex.join([str(Path(sys.executable).resolve()), str(SOURCE / "bridge.py"),
                                           "--runtime", str(runtime)]),
              "GUIDE_PATH": str(ROOT / "docs/conversations.md"),
              "PEER_MODE": ("Use --manual-peer on loop-begin. Give the user the exact returned loop-peer command "
                            "to run in another terminal before publishing." if manual else
                            "Use automatic window opening. Do not ask the user to run loop-peer.")}
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def manage_command(project, runtime, manual=False, remove=False, check_only=False):
    path = project / ".claude/commands/codex-loop.md"
    marker = "<!-- Managed by claude-codex-bridge: " + str(ROOT) + " -->"
    expected = render_command(runtime, manual).replace("\n---\n", "\n---\n\n" + marker + "\n", 1)
    if path.is_symlink() or any(parent.is_symlink() for parent in (path.parent, path.parent.parent)):
        raise BridgeError("Refusing to install a command through a symlink")
    if path.exists() and marker not in path.read_text().splitlines()[:8]:
        raise BridgeError(f"An unrelated /codex-loop command already exists: {path}. Keep it or move it explicitly")
    if check_only:
        return path
    if remove:
        if path.exists():
            path.unlink()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic(path, expected.encode())
    return path


def install(args):
    project = Path(args.project).expanduser().resolve(strict=True)
    if not project.is_dir():
        raise BridgeError("Project must be an existing directory")
    runtime = Path(args.runtime).expanduser().resolve()
    manage_command(project, runtime, args.manual_peer, check_only=True)
    settings = project / ".claude/settings.local.json"
    bridge = Bridge(runtime)
    if args.uninstall:
        bridge.install(settings, remove=True)
        manage_command(project, runtime, remove=True)
        return {"removed_from": str(project), "runtime_preserved": str(runtime)}
    base = [sys.executable, str(SOURCE / "bridge.py"), "--runtime", str(runtime)]
    def run(*arguments):
        result = subprocess.run([*base, *arguments], text=True, capture_output=True, timeout=90)
        if result.returncode:
            raise BridgeError(result.stderr.strip() or result.stdout.strip())
    run("configure", *(["--codex", args.codex] if args.codex else []))
    if not args.manual_peer:
        flags = []
        if args.wt: flags += ["--wt", args.wt]
        if args.distribution: flags += ["--distribution", args.distribution]
        run("configure-terminal", *flags)
    with lock(runtime / "config.lock"):
        config = bridge.config()
        target = config["projects"].setdefault(str(project), {"artifact_roots": [str(project)]})
        target["enabled"] = True
        if not (project / ".git").exists():
            target["conversation_only"] = True
        atomic(bridge.config_file, config)
    bridge.install(settings)
    path = manage_command(project, runtime, args.manual_peer)
    return {"project": str(project), "command": str(path), "settings": str(settings),
            "runtime": str(runtime), "automatic_terminal": not args.manual_peer,
            "next_step": "Open Claude Code in this project, then invoke /codex-loop with one task"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--runtime", default=str(DEFAULT_RUNTIME))
    parser.add_argument("--codex")
    parser.add_argument("--manual-peer", action="store_true")
    parser.add_argument("--wt")
    parser.add_argument("--distribution")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(install(args), indent=2))
    except (BridgeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
