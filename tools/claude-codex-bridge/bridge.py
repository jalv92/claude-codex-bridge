#!/usr/bin/env python3
"""Opt-in Claude–Codex saved reviews and bounded task conversations (Linux/WSL, stdlib only)."""
import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from contracts import (BridgeError, ID, MANIFEST, REQUEST, RESULT, digest, metadata,
                       read_json, validate, validate_prompt, validate_result)
from runtime import (alive, atomic, claude_parent, descendants, git, lock, now,
                     process, snapshot, sync_dir, terminate_owned)

SOURCE = Path(__file__).resolve().parent
WORKSPACE = SOURCE.parents[1]
DEFAULT_RUNTIME = WORKSPACE / "tmp/claude-codex-bridge"
REQUIRED_FLAGS = ("--ignore-user-config", "--ignore-rules", "--output-schema", "--json",
                  "--output-last-message", "--ephemeral", "--skip-git-repo-check")
DISABLED_FEATURES = ("apps", "plugins", "hooks", "multi_agent", "multi_agent_v2",
                     "browser_use", "browser_use_external", "computer_use",
                     "image_generation", "shell_snapshot", "memories",
                     "skill_search", "skill_mcp_dependency_install", "remote_plugin",
                     "daemon_auto_start")
TERMINAL = {"COMPLETED", "FAILED", "TIMED_OUT", "INTERRUPTED", "CANCELLED"}


def canonical(value):
    return Path(value).expanduser().resolve(strict=True)


def emit(value):
    print(json.dumps(value, indent=2))


def child_env(config):
    # Explicit allowlist: no inherited API keys, Claude/Codex session IDs, shell init,
    # proxies, injected NODE_OPTIONS, BASH_ENV, Git config, or tool credentials.
    return {"HOME": str(Path.home()), "USER": os.environ.get("USER", ""),
            "PATH": config["path"], "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "CODEX_HOME": config["codex_home"], "CLAUDE_CODEX_BRIDGE_CHILD": "1",
            "PYTHONDONTWRITEBYTECODE": "1"}


def executable(path):
    p = canonical(path)
    if not p.is_file() or not os.access(p, os.X_OK):
        raise BridgeError(f"Not executable: {p}")
    with p.open("rb") as f:
        if f.read(2) == b"MZ":
            raise BridgeError("Windows executables are not supported by this WSL bridge")
    return str(p)


def discover_codex():
    h = Path.home()
    roots = [h / ".bun/install/global/node_modules"]
    roots += list((h / ".nvm/versions/node").glob("*/lib/node_modules"))
    for root in roots:
        for p in sorted(root.glob("@openai/codex*/**/bin/codex"), reverse=True):
            if p.is_file():
                with p.open("rb") as f:
                    if f.read(4) == b"\x7fELF":
                        return executable(p)
    found = shutil.which("codex")
    if not found:
        raise BridgeError("Native Codex was not found; pass --codex /absolute/path")
    return executable(found)


def probe(config):
    def call(*args):
        return subprocess.run([config["codex"], *args], env=child_env(config),
                              capture_output=True, text=True, timeout=15)
    version = call("--version")
    help_result = call("exec", "--help")
    missing = [flag for flag in REQUIRED_FLAGS if flag not in help_result.stdout]
    if version.returncode or help_result.returncode or missing:
        raise BridgeError(f"Codex capability check failed; missing flags: {missing}")
    features = call("features", "list")
    names = {line.split()[0] for line in features.stdout.splitlines() if line.split()}
    missing_features = (set(DISABLED_FEATURES) | {"skip_host_skill_discovery"}) - names
    if features.returncode or missing_features:
        raise BridgeError(f"Cannot enforce integration isolation: {sorted(missing_features)}")
    return version.stdout.strip()


class Bridge:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.config_file = self.root / "config.json"

    def config(self):
        c = read_json(self.config_file)
        if c.get("schema_version") != 1 or not c.get("enabled"):
            raise BridgeError("Bridge is disabled or configuration is invalid")
        return c

    def job(self, job_id):
        validate(job_id, ID)
        p = self.root / "jobs" / job_id
        if not p.is_dir() or p.is_symlink():
            raise BridgeError("Unknown job")
        return p

    def state(self, job):
        return read_json(job / "state.json")

    def save(self, job, record, **updates):
        record.update(updates, updated_at=now())
        atomic(job / "state.json", record)
        return record

    def target(self, project):
        c = self.config()
        p = canonical(project)
        target = c["projects"].get(str(p))
        if not target or not target["enabled"]:
            raise BridgeError(f"Project is not enabled: {p}")
        if canonical(git(p, "rev-parse", "--show-toplevel")) != p:
            raise BridgeError("Project must be the independent Git repository root")
        return p, target

    def configure(self, args):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        # Runtime must never accidentally become versioned infrastructure.
        if self.root.is_relative_to(WORKSPACE):
            git(WORKSPACE, "check-ignore", "--quiet", str(self.root / "config.json"))
        with lock(self.root / "config.lock"):
            c = read_json(self.config_file) if self.config_file.exists() else {
                "schema_version": 1, "enabled": True, "projects": {},
                "codex_home": str(Path.home() / ".codex"), "model": None,
                "max_timeout_seconds": 1800, "copy_limit_bytes": 16 * 1024 * 1024}
            c["codex"] = executable(args.codex) if args.codex else c.get("codex") or discover_codex()
            dirs = [str(Path(c["codex"]).parent)]
            node = shutil.which("node")
            if node:
                dirs.append(str(Path(node).resolve().parent))
            # Python/Git and bounded reproduction utilities have stable locations.
            c["path"] = ":".join(dict.fromkeys(dirs + ["/usr/local/bin", "/usr/bin", "/bin"]))
            claude = shutil.which("claude")
            if not claude:
                candidates = sorted((Path.home() / ".nvm/versions/node").glob("*/bin/claude"), reverse=True)
                claude = str(candidates[0]) if candidates else None
            if claude:
                c["claude"] = executable(claude)
            if args.model:
                c["model"] = args.model
            if c["model"] is None:
                import tomllib
                user_config = Path(c["codex_home"]) / "config.toml"
                if user_config.exists():
                    c["model"] = tomllib.loads(user_config.read_text()).get("model")
            if args.project:
                p = canonical(args.project)
                if canonical(git(p, "rev-parse", "--show-toplevel")) != p:
                    raise BridgeError("Project must have its own Git repository")
                roots = [str(p)] + [str(canonical(r)) for r in args.artifact_root]
                previous = c["projects"].get(str(p), {})
                c["projects"][str(p)] = {"enabled": True,
                    "artifact_roots": sorted(set(roots + previous.get("artifact_roots", [])))}
            c["verified_codex_version"] = probe(c)
            atomic(self.config_file, c)
        return {"configuration": str(self.config_file), "codex": c["codex"],
                "version": c["verified_codex_version"], "projects": c["projects"]}

    def current_session(self):
        parent = claude_parent()
        if not parent:
            raise BridgeError("No originating Claude process found. Run arm inside Claude after the identity hook fires, or supply --session and --session-cwd explicitly")
        key = f"{parent['boot']}-{parent['pid']}-{parent['start']}"
        p = self.root / "sessions" / (key + ".json")
        if not p.exists():
            raise BridgeError("Session identity is unavailable. Reload Claude hooks and send a new prompt before arming")
        identity = read_json(p)
        if not alive(identity["process"]):
            raise BridgeError("Stale Claude session identity")
        return identity["session_id"], identity["cwd"]

    def arm(self, args):
        project, _ = self.target(args.project)
        if args.session:
            validate(args.session, ID)
            if not args.session_cwd:
                raise BridgeError("Explicit --session also requires --session-cwd")
            session_id, cwd = args.session, str(canonical(args.session_cwd))
        else:
            session_id, cwd = self.current_session()
        if not (project.is_relative_to(cwd) or Path(cwd).is_relative_to(project)):
            raise BridgeError("Session directory is unrelated to the project")
        if not args.task.strip():
            raise BridgeError("Task identity cannot be empty")
        base = git(project, "rev-parse", "HEAD")
        self.root.joinpath("jobs").mkdir(exist_ok=True)
        with lock(self.root / "arm.lock"):
            for path in (self.root / "jobs").glob("*/state.json"):
                s = read_json(path)
                if (s["session_id"], s["project"], s["task_id"]) == (session_id, str(project), args.task):
                    if s["state"] not in TERMINAL:
                        return {"job_id": s["job_id"], "state": s["state"],
                                "draft": str(path.parent / "draft")}
            job_id = uuid.uuid4().hex
            job = self.root / "jobs" / job_id
            (job / "draft").mkdir(parents=True, mode=0o700)
            state = dict(job_id=job_id, session_id=session_id, session_cwd=cwd,
                         task_id=args.task, project=str(project), state="ARMED",
                         created_at=now(), base_commit=base,
                         base_branch=git(project, "rev-parse", "--abbrev-ref", "HEAD"), attempt=0)
            self.save(job, state)
            shutil.copyfile(SOURCE / "templates/review-prompt.md", job / "draft/prompt.md")
            atomic(job / "draft/request.json", {"schema_version": 1, "language": "en",
                "prompt": str(job / "draft/prompt.md"), "base_commit": base,
                "final_commit": base, "scope": {"description": "REPLACE_ME",
                "allowed_commands": [], "scratch_write": False}, "timeout_seconds": 600,
                "artifacts": []})
        return {"job_id": job_id, "state": "ARMED", "draft": str(job / "draft"),
                "request": str(job / "draft/request.json"), "base_commit": base}

    def allowed_artifact(self, raw, target):
        p = canonical(raw)
        if not p.is_file() or not any(p.is_relative_to(canonical(r)) for r in target["artifact_roots"]):
            raise BridgeError(f"Artifact is outside the configured file scope: {p}")
        if any(x in p.parts for x in (".git", ".ssh", ".codex", ".claude")) or p.name.startswith(".env"):
            raise BridgeError(f"Credentials/configuration cannot be review artifacts: {p}")
        return p

    def submit(self, args):
        job = self.job(args.job)
        with lock(job / "lock"):
            state = self.state(job)
            if state["state"] != "ARMED":
                raise BridgeError("Only an ARMED job can be submitted; accepted packages are immutable")
            project, target = self.target(state["project"])
            request = read_json(args.request or job / "draft/request.json")
            validate(request, REQUEST)
            if len(request["artifacts"]) > 128:
                raise BridgeError("At most 128 artifacts are accepted per package")
            if request["base_commit"] != state["base_commit"]:
                raise BridgeError("Base must equal the commit captured when the task was armed")
            if request["timeout_seconds"] > self.config()["max_timeout_seconds"]:
                raise BridgeError("Timeout exceeds the configured maximum")
            if "REPLACE_ME" in request["scope"]["description"]:
                raise BridgeError("Complete the review scope")
            for key in ("base_commit", "final_commit"):
                resolved = git(project, "rev-parse", "--verify", request[key] + "^{commit}")
                if resolved != request[key]:
                    raise BridgeError("Commits must use full object IDs")
            if request["final_commit"] != git(project, "rev-parse", "HEAD"):
                raise BridgeError("Final commit must be current HEAD at publication")
            git(project, "merge-base", "--is-ancestor", request["base_commit"], request["final_commit"])
            # Reject tracked edits; unrelated untracked files are recorded and excluded.
            if git(project, "status", "--porcelain", "--untracked-files=no"):
                raise BridgeError("Tracked changes remain uncommitted. Commit explicitly or stop; the bridge never commits/stashes/resets")
            prompt = canonical(request["prompt"])
            if not prompt.is_relative_to(job / "draft"):
                raise BridgeError("Prompt must be saved inside this job's draft directory")
            text = prompt.read_text()
            validate_prompt(text)
            # Unique staging path: interrupted submissions never publish partial readiness.
            stage = job / (".package-" + uuid.uuid4().hex)
            stage.mkdir(mode=0o700)
            atomic(stage / "prompt.md", text.encode())
            atomic(stage / "reviewer.md", (SOURCE / "templates/reviewer.md").read_bytes())
            atomic(stage / "result.schema.json", RESULT)
            artifacts = []
            copied_bytes = 0
            for i, spec in enumerate(request["artifacts"]):
                source = self.allowed_artifact(spec["path"], target)
                before = metadata(source)
                sha = digest(source)
                if spec["mode"] == "copy":
                    copied_bytes += before["size"]
                    if before["size"] > self.config()["copy_limit_bytes"]:
                        raise BridgeError("Large artifacts must use mode=reference")
                    if copied_bytes > 32 * 1024 * 1024:
                        raise BridgeError("Copied artifacts exceed the 32 MiB package limit; use references")
                    rel = f"artifacts/{i:04d}"
                    (stage / "artifacts").mkdir(exist_ok=True)
                    shutil.copyfile(source, stage / rel)
                    if digest(stage / rel) != sha:
                        raise BridgeError("Artifact changed while being copied")
                else:
                    rel = str(source)
                if metadata(source) != before:
                    raise BridgeError("Artifact changed during submission")
                artifacts.append(dict(source=str(source), path=rel, sha256=sha,
                                      stat=before, mode=spec["mode"], purpose=spec["purpose"]))
            manifest = {"schema_version": 1, **{k: state[k] for k in (
                "job_id", "session_id", "task_id", "session_cwd", "project", "created_at", "base_branch")},
                "readiness": "READY_FOR_REVIEW", "language": "en",
                "base_commit": request["base_commit"], "final_commit": request["final_commit"],
                "final_branch": git(project, "rev-parse", "--abbrev-ref", "HEAD"),
                "prompt": {"path": "prompt.md", "sha256": digest(stage / "prompt.md")},
                "review_policy": {"path": "reviewer.md", "sha256": digest(stage / "reviewer.md")},
                "result_schema": {"path": "result.schema.json", "sha256": digest(stage / "result.schema.json")},
                "scope": request["scope"], "timeout_seconds": request["timeout_seconds"],
                "artifacts": artifacts}
            validate(manifest, MANIFEST)
            atomic(stage / "manifest.json", manifest)
            seal = digest(stage / "manifest.json")
            for p in stage.rglob("*"):
                if p.is_file():
                    with p.open("rb") as f:
                        os.fsync(f.fileno())
                    p.chmod(0o444)
            stage.rename(job / "package")
            sync_dir(job)
            self.save(job, state, state="READY", manifest_sha256=seal,
                      published_at=now(), excluded_untracked=git(project, "ls-files", "--others", "--exclude-standard").splitlines())
        return {"job_id": args.job, "state": "READY", "readiness": "READY_FOR_REVIEW",
                "package": str(job / "package"), "dispatch": "Waiting for the originating Claude Stop event"}

    def package(self, job, state):
        path = job / "package"
        if digest(path / "manifest.json") != state["manifest_sha256"]:
            raise BridgeError("Accepted manifest changed")
        m = read_json(path / "manifest.json")
        validate(m, MANIFEST)
        for key in ("prompt", "review_policy", "result_schema"):
            frozen = path / m[key]["path"]
            if frozen.is_symlink() or digest(frozen) != m[key]["sha256"]:
                raise BridgeError(f"Accepted {key} changed")
        for key in ("job_id", "session_id", "session_cwd", "task_id", "project", "base_commit"):
            if m[key] != state[key]:
                raise BridgeError(f"Manifest identity mismatch: {key}")
        _, target = self.target(m["project"])
        validate_prompt((path / "prompt.md").read_text())
        for i, a in enumerate(m["artifacts"]):
            # Validate paths even though the manifest seal binds accepted metadata.
            if a["mode"] == "copy":
                if a["path"] != f"artifacts/{i:04d}" or (path / a["path"]).is_symlink():
                    raise BridgeError("Invalid frozen artifact path")
                if digest(path / a["path"]) != a["sha256"]:
                    raise BridgeError("Frozen artifact changed")
            else:
                if a["path"] != a["source"] or not any(
                        Path(a["source"]).is_relative_to(Path(r)) for r in target["artifact_roots"]):
                    raise BridgeError("Reference outside the enabled artifact roots")
        return m

    def reference_limits(self, manifest, full=True):
        limits = []
        for a in manifest["artifacts"]:
            if a["mode"] != "reference":
                continue
            p = Path(a["source"])
            try:
                if str(p.resolve(strict=True)) != a["source"] or metadata(p) != a["stat"]:
                    limits.append(f"Reference unavailable or changed since publication: {p}")
                elif full and (digest(p) != a["sha256"] or metadata(p) != a["stat"]):
                    limits.append(f"Reference integrity mismatch: {p}")
            except OSError:
                limits.append(f"Reference unavailable: {p}")
        return limits

    def refresh(self, job, s):
        if s["state"] == "RUNNING" and not alive(s.get("worker")):
            # A status caller may be in a different PID namespace. The lifetime lock
            # remains authoritative even when /proc cannot see the host worker.
            with lock(job / "worker.lock", blocking=False) as acquired:
                if not acquired:
                    return s
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(s["claimed_at"])).total_seconds()
            if s.get("phase") in ("DISPATCHING", "STARTING") and age < 10:
                return s
            self.save(job, s, state="INTERRUPTED", error="Worker is no longer alive; explicit run --recover is required",
                      finished_at=now())
        return s

    def launch(self, job, recover=False):
        with lock(job / "lock"):
            s = self.refresh(job, self.state(job))
            if s["state"] != "READY":
                if not recover or s["state"] not in ("INTERRUPTED", "FAILED", "TIMED_OUT"):
                    raise BridgeError(f"Job is {s['state']}; only READY or explicit recovery may run")
                if alive(s.get("worker")):
                    raise BridgeError("Worker is still alive")
                identity = s.get("worker") or s.get("codex_process")
                current = process(os.getpid())
                if identity and identity.get("pid_namespace", current["pid_namespace"]) != current["pid_namespace"] \
                        and identity["boot"] == current["boot"]:
                    raise BridgeError("Cannot inspect the original process namespace. Run recovery from the same WSL environment as the job")
                terminate_owned(s.get("children", []) + [s.get("codex_process")])
                if s.get("attempt_dir"):
                    atomic(Path(s["attempt_dir"]) / "status.json", s)
            m = self.package(job, s)
            changed = self.reference_limits(m, full=False)
            if changed:
                self.save(job, s, state="FAILED", error="; ".join(changed), finished_at=now())
                raise BridgeError("Referenced artifacts changed; create a new package")
            token = uuid.uuid4().hex
            attempt = s["attempt"] + 1
            attempt_dir = job / f"attempt-{attempt:03d}"
            attempt_dir.mkdir()
            self.save(job, s, state="RUNNING", phase="DISPATCHING", token=token,
                      attempt=attempt, attempt_dir=str(attempt_dir), claimed_at=now(),
                      worker=None, codex_process=None, children=[], error=None,
                      exit_code=None, worker_exit_code=None, codex_session_id=None,
                      started_at=None, heartbeat_at=None,
                      report=None, verdict=None, finished_at=None)
            try:
                with (attempt_dir / "worker.log").open("ab", buffering=0) as log:
                    proc = subprocess.Popen([str(Path(sys.executable).resolve()), str(SOURCE / "bridge.py"),
                        "--runtime", str(self.root), "_worker", s["job_id"], token],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=self.root,
                        env=child_env(self.config()), start_new_session=True, close_fds=True)
                self.save(job, s, worker=process(proc.pid), phase="STARTING")
            except OSError as exc:
                self.save(job, s, state="FAILED", error=f"Worker launch failed: {exc}", finished_at=now())
                raise BridgeError(s["error"]) from exc
        return proc

    def hook(self, event):
        if os.environ.get("CLAUDE_CODEX_BRIDGE_CHILD") or not self.config_file.exists():
            return
        self.config()
        if not isinstance(event, dict) or event.get("agent_id"):
            return
        name = event.get("hook_event_name")
        if name not in ("Stop", "UserPromptSubmit"):
            return
        validate(event.get("session_id"), ID)
        cwd = str(canonical(event["cwd"]))
        if name == "UserPromptSubmit":
            from conversation import Conversations
            Conversations(self).user_prompt(event)
            parent = claude_parent()
            if parent:
                key = f"{parent['boot']}-{parent['pid']}-{parent['start']}"
                atomic(self.root / "sessions" / (key + ".json"),
                       {"session_id": event["session_id"], "cwd": cwd,
                        "process": parent, "observed_at": now()})
            return
        if event.get("stop_hook_active") is not False:
            return
        for path in sorted((self.root / "jobs").glob("*/state.json")):
            s = read_json(path)
            if s["state"] == "READY" and s["session_id"] == event["session_id"] and s["session_cwd"] == cwd:
                try:
                    self.launch(path.parent)
                except (BridgeError, OSError, ValueError) as exc:
                    # Concurrent Stop events are expected. Never block Claude or relaunch.
                    with lock(path.parent / "lock"):
                        current = self.state(path.parent)
                        if current["state"] == "READY":
                            self.save(path.parent, current, state="FAILED", error=str(exc), finished_at=now())

    def install(self, settings, remove=False):
        settings = Path(settings).expanduser().absolute()
        settings.parent.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = hashlib.sha256(str(settings).encode()).hexdigest()
        with lock(self.root / ("settings-" + key + ".lock")):
            before = settings.read_bytes() if settings.exists() else b"{}\n"
            data = json.loads(before)
            command = shlex.join([str(Path(sys.executable).resolve()), str(SOURCE / "bridge.py"),
                                  "--runtime", str(self.root), "hook"])
            loop_command = command[:-4] + "loop-hook"
            owned_commands = {command, loop_command}
            for event in ("UserPromptSubmit", "Stop"):
                groups = data.setdefault("hooks", {}).setdefault(event, [])
                for group in list(groups):
                    had_own_entry = any(h.get("command") in owned_commands for h in group.get("hooks", []))
                    group["hooks"] = [h for h in group.get("hooks", []) if h.get("command") not in owned_commands]
                    if had_own_entry and not group["hooks"]:
                        groups.remove(group)
                if not remove:
                    groups.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
                    if event == "Stop":
                        groups.append({"hooks": [{"type": "command", "command": loop_command,
                                                   "asyncRewake": True, "timeout": 7210}]})
                elif not groups:
                    del data["hooks"][event]
            if not data["hooks"]:
                del data["hooks"]
            if data != json.loads(before):
                backups = self.root / "settings-backups"
                backups.mkdir(parents=True, exist_ok=True)
                atomic(backups / (uuid.uuid4().hex + ".json"), before)
                if settings.exists() and settings.read_bytes() != before:
                    raise BridgeError("Settings changed concurrently; retry installation")
                atomic(settings, data)
        return {"settings": str(settings), "installed": not remove,
                "reload": "Check /hooks in the current Claude session before the next task"}

    def status(self, job_id=None):
        jobs = [self.job(job_id)] if job_id else sorted((self.root / "jobs").glob("*"))
        rows = []
        for job in jobs:
            if not (job / "state.json").exists():
                continue
            with lock(job / "lock"):
                s = self.refresh(job, self.state(job))
            rows.append({**s, "job_directory": str(job)})
        return rows

    def doctor(self):
        c = self.config()
        version = probe(c)
        auth = subprocess.run([c["codex"], "login", "status"], env=child_env(c),
                              capture_output=True, text=True, timeout=15)
        claude_info = {"executable": c.get("claude"), "version": None, "authentication_ready": None}
        if c.get("claude"):
            r = subprocess.run([c["claude"], "--version"], env=child_env(c),
                               capture_output=True, text=True, timeout=15)
            claude_info["version"] = r.stdout.strip() if r.returncode == 0 else "Version probe failed"
            r = subprocess.run([c["claude"], "auth", "status"], env=child_env(c),
                               capture_output=True, text=True, timeout=15)
            try:
                claude_info["authentication_ready"] = json.loads(r.stdout).get("loggedIn", False)
            except ValueError:
                claude_info["authentication_ready"] = False
        settings = [Path.home() / ".claude/settings.json", WORKSPACE / ".claude/settings.json",
                    WORKSPACE / ".claude/settings.local.json", Path("/etc/claude-code/managed-settings.json")]
        settings += [Path(p) / ".claude/settings.local.json" for p in c["projects"]]
        hooks = []
        for p in settings:
            if p.exists():
                d = read_json(p)
                hooks.append({"path": str(p), "disableAllHooks": d.get("disableAllHooks", False),
                    "allowManagedHooksOnly": d.get("allowManagedHooksOnly", False),
                    "events": {k: sum(len(g.get("hooks", [])) for g in v)
                               for k, v in d.get("hooks", {}).items()},
                    "bridge_entries": sum("claude-codex-bridge/bridge.py" in h.get("command", "")
                        for groups in d.get("hooks", {}).values() for g in groups for h in g.get("hooks", []))})
        server_help = subprocess.run([c["codex"], "app-server", "--help"], env=child_env(c),
            capture_output=True, text=True, timeout=15)
        cli_help = subprocess.run([c["codex"], "--help"], env=child_env(c),
            capture_output=True, text=True, timeout=15)
        return {"platform": sys.platform, "runtime": str(self.root), "python": sys.executable,
                "codex": c["codex"], "version": version, "pinned_model": c["model"],
                "authentication_ready": auth.returncode == 0, "path": c["path"],
                "claude": claude_info,
                "projects": c["projects"], "hook_settings": hooks,
                "conversation_mode": {"private_unix_server_available": server_help.returncode == 0 and "unix://" in server_help.stdout,
                    "native_remote_console_available": cli_help.returncode == 0 and "--remote" in cli_help.stdout,
                    "automatic_terminal": c.get("terminal"),
                    "tested_versions": "Codex 0.167.0; Claude Code 2.1.319",
                    "callback_requirement": "Claude asyncRewake; verify the loop-hook Stop entry in /hooks"},
                "limitations": ["Hook execution has to be observed in Claude; installed settings alone do not prove it fired",
                                 "Same-user local integrity checks are not a security boundary against a malicious local owner",
                                 "Workers do not survive WSL shutdown or machine restart"]}


def codex_command(config, attempt, m):
    command = [config["codex"], "exec", "--ignore-user-config", "--ignore-rules",
               "--strict-config", "--ephemeral", "--skip-git-repo-check", "--json",
               "--color", "never", "--output-schema", str(attempt / "result.schema.json"),
               "--output-last-message", str(attempt / "result.raw.json"), "-C", str(attempt / "final")]
    overrides = {"approval_policy": '"never"', "web_search": '"disabled"',
                 "allow_login_shell": "false", "agents.enabled": "false",
                 "project_doc_max_bytes": "0", "project_root_markers": "[]",
                 "skills.bundled.enabled": "false", "skills.include_instructions": "false",
                 "shell_environment_policy.inherit": '"core"',
                 "shell_environment_policy.set.CLAUDE_CODEX_BRIDGE_CHILD": '"1"',
                 "shell_environment_policy.set.PYTHONDONTWRITEBYTECODE": '"1"',
                 "shell_environment_policy.set.TMPDIR": json.dumps(str(attempt / "scratch")),
                 "shell_environment_policy.set.PATH": json.dumps(config["path"])}
    # No project configuration/agent instructions are trusted for the audit launch.
    overrides["projects"] = "{" + ",".join(
        f'{json.dumps(str(parent))} = {{trust_level = "untrusted"}}'
        for parent in [attempt / "final", *attempt.parents]) + "}"
    if m["scope"]["scratch_write"]:
        overrides.update({"default_permissions": '"bridge_review"',
            "permissions": '{bridge_review = {extends = ":read-only", filesystem = {' +
                json.dumps(str(attempt / "scratch")) + ' = "write"}, network = {enabled = false}}}'})
    else:
        command += ["--sandbox", "read-only"]
    for key, value in overrides.items():
        command += ["-c", f"{key}={value}"]
    for feature in DISABLED_FEATURES:
        command += ["--disable", feature]
    command += ["--enable", "skip_host_skill_discovery"]
    if config["model"]:
        command += ["--model", config["model"]]
    return command + ["-"]


def review_input(job, attempt, m):
    header = (job / "package/reviewer.md").read_text()
    # The package is task input; all repository/artifact text remains evidence, not policy.
    context = {"job_id": m["job_id"], "base_commit": m["base_commit"], "final_commit": m["final_commit"],
               "original_project": m["project"], "base_snapshot": str(attempt / "base"),
               "final_snapshot": str(attempt / "final"), "diff": str(attempt / "changes.diff"),
               "scratch": str(attempt / "scratch"), "scope": m["scope"],
               "artifacts": [{**a, "review_path": str(job / "package" / a["path"])
                              if a["mode"] == "copy" else a["path"]} for a in m["artifacts"]]}
    # JSON escaping preserves exact metacharacters without creating executable syntax.
    return header + "\nReview context (JSON data):\n" + json.dumps(context, indent=2) + \
        "\nSubmitted review brief (JSON string data):\n" + json.dumps((job / "package/prompt.md").read_text())


def write_report(attempt, result):
    validate_result(result)
    result["findings"].sort(key=lambda f: f["priority"])
    atomic(attempt / "result.json", result)
    lines = ["# Independent Codex review", "", f"**Verdict: {result['verdict']}**", "", result["summary"],
             "", "## Findings", ""]
    for f in result["findings"]:
        ref = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
        lines += [f"### {f['priority']} — {f['title']}", "", f"Location: `{ref or 'Not applicable'}`", "", f["detail"], ""]
    if not result["findings"]:
        lines += ["No unresolved findings reported.", ""]
    lines += ["## Checks performed", ""]
    lines += [f"- {c['check']}: {c['result']}" for c in result["checks_performed"]]
    lines += ["", "## Limitations", ""] + [f"- {s}" for s in result["limitations"]]
    atomic(attempt / "report.md", ("\n".join(lines) + "\n").encode())


def worker(bridge, job_id, token):
    job = bridge.job(job_id)
    # Lifetime lock distinguishes live work from a duplicated internal invocation.
    with lock(job / "worker.lock", blocking=False) as acquired:
        if not acquired:
            return
        with lock(job / "lock"):
            s = bridge.state(job)
            if s["state"] != "RUNNING" or s.get("token") != token:
                return
            bridge.save(job, s, worker=process(os.getpid()), phase="VALIDATING", started_at=now())
        attempt = Path(s["attempt_dir"])
        proc = None
        deadline = None
        result = None
        terminal = "FAILED"
        error = None
        def interrupted(signum, frame):
            raise InterruptedError(f"Worker received signal {signum}")
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        # Reparent detached tool descendants to this short-lived worker if possible.
        import ctypes
        ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER
        try:
            config = bridge.config()
            m = bridge.package(job, s)
            deadline = time.monotonic() + m["timeout_seconds"]
            signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError("Review wall-clock timeout")))
            signal.setitimer(signal.ITIMER_REAL, m["timeout_seconds"])
            limits = bridge.reference_limits(m)
            if limits:
                raise BridgeError("; ".join(limits))
            version = probe(config)
            snapshot(m["project"], m["base_commit"], attempt / "base")
            snapshot(m["project"], m["final_commit"], attempt / "final")
            (attempt / "scratch").mkdir()
            diff = git(m["project"], "diff", "--no-ext-diff", "--no-textconv", "--binary",
                       m["base_commit"], m["final_commit"], binary=True)
            atomic(attempt / "changes.diff", diff)
            atomic(attempt / "result.schema.json", (job / "package/result.schema.json").read_bytes())
            atomic(attempt / "input.txt", review_input(job, attempt, m).encode())
            command = codex_command(config, attempt, m)
            atomic(attempt / "execution.json", {"executable": config["codex"], "version": version,
                "model": config["model"] or "CLI default; consult codex.stderr.log",
                "arguments": command, "started_at": now(), "scope": m["scope"],
                "permissions": "read-only plus scratch" if m["scope"]["scratch_write"] else "read-only",
                "limitations": ["Read-only protects writes, not confidentiality of other readable local files",
                    "Semantic command scope is an instruction, not a syscall allowlist; the sandbox restricts writes and networking",
                    "Managed system policies remain authoritative and may prevent execution"]})
            with (attempt / "input.txt").open("rb") as inp, \
                    (attempt / "codex.events.jsonl").open("wb") as out, \
                    (attempt / "codex.stderr.log").open("wb") as err:
                proc = subprocess.Popen(command, stdin=inp, stdout=out, stderr=err,
                    cwd=attempt / "final", env=child_env(config), start_new_session=True, close_fds=True)
                with lock(job / "lock"):
                    bridge.save(job, s, phase="REVIEWING", codex_process=process(proc.pid),
                                executable=config["codex"], version=version, model=config["model"])
                while proc.poll() is None:
                    with lock(job / "lock"):
                        bridge.save(job, s, children=descendants(os.getpid()), heartbeat_at=now())
                    time.sleep(min(0.2, max(0.01, deadline - time.monotonic())))
            if proc.returncode:
                raise BridgeError(f"Codex exited with code {proc.returncode}; see codex.stderr.log")
            result = read_json(attempt / "result.raw.json")
            validate_result(result)
            limits = bridge.reference_limits(m)
            # Recheck the frozen inputs too: changes while the model runs invalidate verification.
            bridge.package(job, s)
            if limits:
                result["verdict"] = "NOT_VERIFIABLE"
                result["limitations"].extend(limits)
                result["summary"] = "Artifact integrity changed during review. " + result["summary"]
            write_report(attempt, result)
            terminal = "COMPLETED"
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            terminal, error = "TIMED_OUT", str(exc)
        except (InterruptedError, KeyboardInterrupt) as exc:
            terminal, error = "INTERRUPTED", str(exc)
        except Exception as exc:
            terminal, error = "FAILED", f"{type(exc).__name__}: {exc}"
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            # Own descendants only. This includes tool sessions detached by Codex.
            terminate_owned(descendants(os.getpid()) + s.get("children", []) + [s.get("codex_process")])
            if proc is not None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    error = (error or "") + "; process cleanup did not finish"
            session_id = None
            events = attempt / "codex.events.jsonl"
            if events.exists():
                with events.open(errors="replace") as f:
                    for line in f:
                        try:
                            event = json.loads(line)
                            if event.get("type") == "thread.started":
                                session_id = event.get("thread_id")
                        except ValueError:
                            pass
            with lock(job / "lock"):
                bridge.save(job, s, state=terminal, phase="FINISHED", finished_at=now(), error=error,
                    exit_code=proc.returncode if proc else None, codex_session_id=session_id,
                    worker_exit_code=0 if terminal == "COMPLETED" else 1,
                    verdict=result["verdict"] if terminal == "COMPLETED" else None,
                    report=str(attempt / "report.md") if terminal == "COMPLETED" else None)
            atomic(attempt / "status.json", s)
        return 0 if terminal == "COMPLETED" else 1


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", default=str(DEFAULT_RUNTIME))
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("configure", help="Configure local executables and explicit project opt-in")
    p.add_argument("--codex"); p.add_argument("--model"); p.add_argument("--project")
    p.add_argument("--artifact-root", action="append", default=[])
    sub.add_parser("doctor")
    p = sub.add_parser("arm")
    p.add_argument("--project", required=True); p.add_argument("--task", required=True)
    p.add_argument("--session"); p.add_argument("--session-cwd")
    p = sub.add_parser("submit"); p.add_argument("job"); p.add_argument("--request")
    p = sub.add_parser("status"); p.add_argument("job", nargs="?")
    p = sub.add_parser("run"); p.add_argument("job"); p.add_argument("--recover", action="store_true")
    p.add_argument("--wait", action="store_true")
    p = sub.add_parser("cancel"); p.add_argument("job")
    for name in ("install-hook", "uninstall-hook"):
        p = sub.add_parser(name)
        p.add_argument("--settings", default=str(WORKSPACE / ".claude/settings.local.json"))
    sub.add_parser("hook")
    p = sub.add_parser("_worker"); p.add_argument("job"); p.add_argument("token")
    from conversation import add_commands, dispatch, Conversations
    add_commands(sub)
    args = parser.parse_args()
    bridge = Bridge(args.runtime)
    try:
        if os.environ.get("CLAUDE_CODEX_BRIDGE_CHILD") and args.command not in (
                "_worker", "status", "doctor", "hook", "loop-hook", "loop-status"):
            raise BridgeError("Bridge mutation and dispatch are disabled inside a review child")
        if args.command == "configure":
            emit(bridge.configure(args))
        elif args.command == "doctor":
            emit(bridge.doctor())
        elif args.command == "arm":
            emit(bridge.arm(args))
        elif args.command == "submit":
            emit(bridge.submit(args))
        elif args.command == "status":
            emit(bridge.status(args.job))
        elif args.command == "run":
            proc = bridge.launch(bridge.job(args.job), args.recover)
            if args.wait:
                proc.wait()
            emit(bridge.status(args.job))
        elif args.command == "cancel":
            job = bridge.job(args.job)
            with lock(job / "lock"):
                s = bridge.refresh(job, bridge.state(job))
                if s["state"] not in ("ARMED", "READY"):
                    raise BridgeError("Only unstarted jobs can be cancelled")
                emit(bridge.save(job, s, state="CANCELLED", finished_at=now()))
        elif args.command in ("install-hook", "uninstall-hook"):
            emit(bridge.install(args.settings, args.command == "uninstall-hook"))
        elif args.command in ("hook", "loop-hook"):
            raw = sys.stdin.buffer.read(1_000_001)
            if len(raw) > 1_000_000:
                raise BridgeError("Hook event too large")
            event = json.loads(raw)
            if args.command == "hook":
                bridge.hook(event)
            elif not os.environ.get("CLAUDE_CODEX_BRIDGE_CHILD") and bridge.config_file.exists():
                bridge.config()
                if isinstance(event, dict):
                    message = Conversations(bridge).hook(event)
                    if message:
                        print(message, file=sys.stderr)
                        return 2  # asyncRewake delivers this only to the originating Claude session.
        elif args.command.startswith("loop-") or args.command == "configure-terminal":
            out = dispatch(bridge, args)
            print(out, end="") if isinstance(out, str) else emit(out)
            if args.command == "loop-peer" and args.hold and sys.stdin.isatty():
                try:
                    input("Task finished. The review and status are saved above. Press Enter to close this window. ")
                except (EOFError, KeyboardInterrupt):
                    pass
        elif args.command == "_worker":
            return worker(bridge, args.job, args.token) or 0
    except (BridgeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        if args.command in ("hook", "loop-hook"):
            # Hook failure never blocks Claude or writes a stop decision.
            print(f"Claude-Codex bridge: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 0
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
