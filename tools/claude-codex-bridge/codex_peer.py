"""Task-scoped Codex app-server transport; no global daemon or chat discovery."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tomllib

from contracts import BridgeError, RESULT
from runtime import alive, atomic, descendants, now, process, terminate_owned
from local_socket import LocalSocket


class Rpc:
    def __init__(self, socket_path, log, delegate_requests=False):
        self.delegate_requests = delegate_requests
        self.log = open(log, "a", buffering=1)
        self.transport = LocalSocket(socket_path)
        self.events = queue.Queue()
        self.responses = {}
        self.condition = threading.Condition()
        self.writer = threading.Lock()
        self.counter = 0
        self.closed = False
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            while True:
                event = self.transport.receive()
                self.log.write(json.dumps(event) + "\n")
                if "method" in event and "id" in event:
                    if self.delegate_requests:
                        # The native TUI receives the same request (or replay on resume).
                        # The first reply wins: even an error here would steal its approval.
                        self.events.put(event)
                    else:
                        self.send({"id": event["id"], "error": {
                            "code": -32601, "message": "This review bridge cannot grant permissions or invoke client tools"}})
                elif "id" in event:
                    with self.condition:
                        self.responses[event["id"]] = event
                        self.condition.notify_all()
                else:
                    self.events.put(event)
        except (ValueError, OSError, EOFError, BridgeError):
            pass
        finally:
            with self.condition:
                self.closed = True
                self.condition.notify_all()
            self.events.put({"method": "bridge/connectionClosed"})

    def send(self, message):
        self.transport.send(message)

    def call(self, method, params, timeout=20):
        with self.condition:
            self.counter += 1
            request_id = self.counter
            self.send({"id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + timeout
            while request_id not in self.responses:
                remaining = deadline - time.monotonic()
                if self.closed or remaining <= 0:
                    raise BridgeError(f"Codex RPC {method}: connection closed or acknowledgement timed out; delivery may be ambiguous")
                self.condition.wait(remaining)
            response = self.responses.pop(request_id)
        if "error" in response:
            raise BridgeError(f"Codex RPC {method}: {response['error']}")
        return response["result"]

    def close(self):
        self.transport.close()
        self.reader.join(timeout=2)
        self.log.close()


def reviewer_config(config, project):
    from bridge import DISABLED_FEATURES
    user_path = Path(config["codex_home"]) / "config.toml"
    user = tomllib.loads(user_path.read_text()) if user_path.exists() else {}
    features = {name: False for name in DISABLED_FEATURES}
    features["skip_host_skill_discovery"] = True
    return {"approval_policy": "never", "sandbox_mode": "read-only", "web_search": "disabled",
            "allow_login_shell": False, "agents": {"enabled": False}, "notify": [],
            "features": features, "project_doc_max_bytes": 0, "project_root_markers": [],
            "skills": {"bundled": {"enabled": False}, "include_instructions": False},
            "projects": {str(p): {"trust_level": "untrusted"} for p in [project, *project.parents]},
            "mcp_servers": {name: {"enabled": False} for name in user.get("mcp_servers", {})}}


def toml_value(value):
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + "=" + toml_value(v) for k, v in value.items()) + "}"
    return json.dumps(value)


def native_config():
    # Preserve CLI tools, MCP, apps, skills, model and approval configuration.
    # Lifecycle hooks/notify could recursively dispatch work outside this task.
    return {"features": {"hooks": False}, "notify": []}


# Proposal mode: writes only inside the scratch cwd and temp dirs, no network, no escalation.
PROPOSE_SANDBOX = {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
                   "excludeTmpdirEnvVar": False, "excludeSlashTmp": False}


def native_env(config):
    from bridge import child_env
    env = dict(os.environ)
    for name in list(env):
        if name.startswith(("CLAUDE", "CODEX_")) or name in ("BASH_ENV", "ENV", "NODE_OPTIONS"):
            env.pop(name)
    env.update(child_env(config))
    return env


class CodexPeer:
    propose = False
    last_ui_exit = None

    def __init__(self, config, directory, project, instructions, visible=True):
        from bridge import child_env
        import ctypes
        # The dedicated peer process owns this server and adopts detached tool descendants.
        ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)  # Linux PR_SET_CHILD_SUBREAPER
        self.directory = directory
        self.project = project
        self.config = config
        self.native = config.get("conversation_mode") in ("native", "propose")
        self.propose = config.get("conversation_mode") == "propose"
        self.visible = visible
        if self.native and not self.propose and not visible:
            raise BridgeError("Native-tool conversations require the visible Codex console for approvals; use --read-only-review for headless reviews")
        self.terminal_settings = termios.tcgetattr(sys.stdin.fileno()) if visible and sys.stdin.isatty() else None
        self.env = native_env(config) if self.native else child_env(config)
        self.env["TERM"] = os.environ.get("TERM", "xterm-256color")
        self.ui = None
        self.rpc = None
        self.server = None
        self.server_identity = None
        # UNIX socket paths are limited to 108 bytes; workspace paths can exceed this.
        self.socket_dir = Path(tempfile.mkdtemp(prefix=f"ccbridge-{os.getuid()}-"))
        self.socket_dir.chmod(0o700)
        self.socket = self.socket_dir / "peer.sock"
        self.log = open(directory / "server.stderr.log", "a", buffering=1)
        overrides = native_config() if self.native else reviewer_config(config, project)
        if self.propose:
            # MCP servers and apps run outside the shell sandbox and could write the live
            # project, so proposal mode switches them off like the read-only reviewer does.
            isolated = reviewer_config(config, project)
            overrides.update(features=isolated["features"], mcp_servers=isolated["mcp_servers"])
            # Pin the policy so a user's config.toml cannot add writable roots or network.
            overrides.update(approval_policy="never", sandbox_workspace_write={"writable_roots": [],
                "network_access": False, "exclude_tmpdir_env_var": False, "exclude_slash_tmp": False})
        self.overrides = overrides
        args = [config["codex"]]
        for k, v in overrides.items():
            args += ["-c", f"{k}={toml_value(v)}"]
        try:
            self.server = subprocess.Popen(args + ["app-server", "--listen", "unix://" + str(self.socket)],
                stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log, env=self.env,
                cwd=project if self.native else directory, start_new_session=True, close_fds=True)
            self.server_identity = process(self.server.pid)
            atomic(directory / "server-process.json", {"server": self.server_identity,
                "started_at": now(), "children": []})
            deadline = time.monotonic() + 20
            while not self.socket.exists():
                if self.server.poll() is not None or time.monotonic() > deadline:
                    raise BridgeError("Private Codex server did not start; see server.stderr.log")
                time.sleep(.05)
            # Headless proposal peers have no console: tool requests are denied, never answered for the user.
            self.rpc = Rpc(self.socket, directory / "protocol.jsonl", delegate_requests=self.native and visible)
            self.rpc.call("initialize", {"clientInfo": {"name": "claude_codex_bridge", "version": "2.0.0"}})
            self.rpc.send({"method": "initialized", "params": {}})
            params = {"cwd": str(project), "config": overrides, "developerInstructions": instructions}
            if self.native:
                # App-server defaults to read-only even with all tools enabled. This
                # task explicitly authorizes project edits; retain the sandbox and
                # inherited approval policy for anything requiring escalation.
                params["sandbox"] = "workspace-write"
                if self.propose:
                    params["approvalPolicy"] = "never"
            else:
                params.update(model=config["model"], approvalPolicy="never", sandbox="read-only")
            result = self.rpc.call("thread/start", params)
            if not self.native and (result.get("sandbox", {}).get("type") != "readOnly" or result.get("sandbox", {}).get("networkAccess") \
                    or result.get("approvalPolicy") != "never"):
                raise BridgeError("Codex did not enforce the required read-only, offline tool sandbox and no escalation")
            if self.propose and (result.get("sandbox") != PROPOSE_SANDBOX or result.get("approvalPolicy") != "never"):
                raise BridgeError("Codex did not enforce the proposal sandbox: scratch-only writes, offline, no escalation")
            self.thread_id = result["thread"]["id"]
            self.thread_path = result["thread"].get("path")
            atomic(directory / "codex-thread.json", {"thread_id": self.thread_id,
                "server": process(self.server.pid), "socket": str(self.socket),
                "effective_config": overrides, "model": result.get("model", config["model"]),
                "reasoning_effort": result.get("reasoningEffort"), "sandbox": result["sandbox"],
                "mode": config.get("conversation_mode") if self.native else "read-only", "approval_policy": result.get("approvalPolicy"),
                "approvals_reviewer": result.get("approvalsReviewer"),
                "executable": config["codex"], "version": subprocess.check_output(
                    [config["codex"], "--version"], env=self.env, text=True, timeout=10).strip(),
                "limitations": (["Proposal mode: shell writes are limited to the scratch copy and temp directories; a human applies or rejects each diff",
                    "MCP servers, apps, plugins and shell network access are disabled",
                    "Reading other local files is not prevented; scope is an instruction"] if self.propose else
                    ["Native CLI tools and approvals are inherited; project writes use workspace-write sandboxing",
                    "Tool availability depends on installed integrations and authentication",
                    "Lifecycle hooks and notify are disabled to prevent recursive dispatch",
                    "Task scope is an instruction, not a filesystem allowlist; Claude must wait while Codex edits"] if self.native else
                    ["App-server loads user configuration; MCP servers, hooks, skills, apps and plugins are overridden off",
                     "Read-only limits writes, not reading other files; semantic reproduction scope is an instruction"])})
        except BaseException:
            self.close()
            raise

    def review(self, prompt, cwd, deadline, check_cancelled):
        while not self.rpc.events.empty():
            self.rpc.events.get_nowait()
        params = {"threadId": self.thread_id,
            "input": [{"type": "text", "text": prompt}], "cwd": str(cwd),
            "outputSchema": RESULT}
        if self.propose:
            params.update(approvalPolicy="never", sandboxPolicy=PROPOSE_SANDBOX)
        elif not self.native:
            params.update(approvalPolicy="never", sandboxPolicy={"type": "readOnly", "networkAccess": False})
        result = self.rpc.call("turn/start", params)
        turn_id = result["turn"]["id"]
        final_text = None
        last_heartbeat = 0
        try:
            if self.visible and self.ui is None:
                # A new thread has no resumable rollout until its first turn is published.
                until = min(deadline, time.monotonic() + 5)
                while self.thread_path and not Path(self.thread_path).exists() and time.monotonic() < until:
                    time.sleep(.05)
                # Remote resume inherits the server policy; local permission overrides are rejected.
                self.ui = subprocess.Popen([self.config["codex"], "--remote", "unix://" + str(self.socket),
                    "--no-alt-screen", "resume", self.thread_id], env=self.env, cwd=self.directory)
            while True:
                if time.monotonic() - last_heartbeat > 1:
                    atomic(self.directory / "server-process.json", {"server": self.server_identity,
                        "observed_at": now(), "children": descendants(self.server.pid)})
                    last_heartbeat = time.monotonic()
                if check_cancelled() or time.monotonic() >= deadline:
                    raise TimeoutError("Review was cancelled or reached its deadline")
                if self.ui is not None and self.ui.poll() is not None:
                    raise InterruptedError("The Codex peer console was closed")
                try:
                    event = self.rpc.events.get(timeout=.2)
                except queue.Empty:
                    continue
                method, params = event.get("method"), event.get("params", {})
                if method == "bridge/connectionClosed":
                    raise BridgeError("Codex connection closed during review; do not silently resend")
                if params.get("threadId") != self.thread_id or params.get("turnId", turn_id) != turn_id:
                    continue
                if method == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage" and item.get("phase") in (None, "final_answer"):
                        final_text = item.get("text")
                if method == "turn/completed" and params["turn"]["id"] == turn_id:
                    turn = params["turn"]
                    if turn["status"] != "completed":
                        raise BridgeError(f"Codex turn failed: {turn.get('error') or turn['status']}")
                    for item in turn.get("items", []):
                        if item.get("type") == "agentMessage" and item.get("phase") in (None, "final_answer"):
                            final_text = item.get("text")
                    if not final_text:
                        raise BridgeError("Codex completed without a structured final message")
                    return json.loads(final_text), turn_id
        except BaseException:
            try:
                self.rpc.call("turn/interrupt", {"threadId": self.thread_id, "turnId": turn_id}, timeout=5)
            except Exception:
                pass
            raise

    def detach_ui(self):
        """Close the native console and hand the terminal back to the bridge.
        The thread stays on the server; the next turn reopens the console."""
        if self.ui is not None:
            if self.ui.poll() is None:
                self.ui.terminate()
                try:
                    self.ui.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.ui.kill()
                    self.ui.wait()
            self.last_ui_exit = self.ui.returncode
            self.ui = None
        if self.terminal_settings is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.terminal_settings)
                sys.stdout.write("\x1b[?2004l\x1b[?1004l\x1b[?25h\x1b[0m")
                sys.stdout.flush()
            except OSError:
                pass  # A closed terminal must not prevent server/process cleanup.

    def close(self):
        self.detach_ui()
        if self.rpc is not None:
            self.rpc.close()
            self.rpc = None
        if self.server is not None:
            terminate_owned([self.server_identity] + descendants(os.getpid()))
            self.server.wait(timeout=5)
            terminate_owned(descendants(os.getpid()))
            atomic(self.directory / "process-status.json", {"closed_at": now(),
                "server": self.server_identity, "server_exit_code": self.server.returncode,
                "ui_exit_code": self.last_ui_exit})
            self.server = None
        self.log.close()
        # Remove only the socket directory created by this peer.
        for p in self.socket_dir.iterdir():
            if p.is_socket():
                p.unlink()
        try:
            self.socket_dir.rmdir()
        except OSError:
            pass
