"""Bounded real-CLI smoke test. No workspace SessionStart hooks or project audits."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from bridge import Bridge, DEFAULT_RUNTIME
from codex_peer import CodexPeer
from runtime import atomic, descendants, process, terminate_owned, now


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--startup-only", action="store_true")
    parser.add_argument("--ui-probe", action="store_true", help="Attach native Codex UI; requires a terminal")
    parser.add_argument("--native-tools", action="store_true", help="With --ui-probe, verify inherited tools and a bounded edit in the fixture")
    parser.add_argument("--automatic-terminal", action="store_true", help="Verify actual automatic Windows Terminal opening from the Stop hook")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    if args.native_tools and not args.ui_probe:
        parser.error("--native-tools requires --ui-probe and a terminal for normal approvals")
    if not 30 <= args.timeout <= 600:
        parser.error("Use a 30..600-second deadline")
    config = Bridge(DEFAULT_RUNTIME).config()
    if args.native_tools:
        config["conversation_mode"] = "native"
    root = SOURCE.parents[1] / "tmp/claude-codex-conversation-smoke" / uuid.uuid4().hex
    repo = root / "calculator fixture"
    repo.mkdir(parents=True)
    (repo / "calculator.py").write_text("def multiply(a, b):\n    return a + b\n")
    for cmd in (["init", "-q"], ["config", "user.email", "fixture@example.invalid"],
                ["config", "user.name", "Bridge fixture"], ["add", "calculator.py"],
                ["commit", "-qm", "Isolated smoke fixture"]):
        subprocess.run(["/usr/bin/git", "-C", str(repo), *cmd], check=True)
    runtime = root / "runtime"
    runtime.mkdir()
    config["projects"] = {str(repo): {"enabled": True, "artifact_roots": [str(repo)],
                                   "max_timeout": 600, "copy_limit_bytes": 16777216}}
    atomic(runtime / "config.json", config)
    print(f"Smoke evidence: {root}", flush=True)
    if args.startup_only or args.ui_probe:
        instructions = ("This is an explicitly authorized, bounded CLI tools test in an isolated calculator fixture. "
            "Use the normal CLI tools and approvals. Only edit calculator.py and run the specified Python assertions. "
            "Do not commit, push, run hooks, call external integrations or change any other project. "
            "The final response must follow the supplied JSON schema." if args.native_tools else
            "Read-only startup probe; do not start any model turn.")
        peer = CodexPeer(config, root, repo, instructions, args.ui_probe)
        try:
            if args.ui_probe and not args.startup_only:
                prompt = ("Inspect calculator.py. Fix multiply to return the mathematical product. "
                    "Edit the file with the available CLI tools, then run exactly: "
                    "python3 -B -c 'from calculator import multiply; assert multiply(3,4)==12; assert multiply(-2,3)==-6; assert multiply(0,9)==0'. "
                    "Return VERIFIED only after the actual edit and successful checks; describe tool or permission limitations honestly."
                    if args.native_tools else
                    "Inspect calculator.py in this fixture. multiply must multiply integers; "
                    "read the file independently, report the bug using the supplied JSON schema. "
                    "Static inspection only; no reproduction commands, no changes.")
                answer, turn = peer.review(prompt, repo, time.monotonic() + min(120, args.timeout), lambda: False)
                assert answer["verdict"] == ("VERIFIED" if args.native_tools else "NEEDS_FIXES"), answer
                if args.native_tools:
                    subprocess.run(["/usr/bin/python3", "-B", "-c", "from calculator import multiply; "
                        "assert multiply(3,4)==12; assert multiply(-2,3)==-6; assert multiply(0,9)==0"], cwd=repo, check=True)
                time.sleep(3)
                if peer.ui.poll() is not None:
                    raise RuntimeError(f"Native Codex UI exited: {peer.ui.returncode}")
                atomic(root / "ui-probe.json", {"result": "PASS", "thread_id": peer.thread_id,
                    "turn_id": turn, "review": answer, "ui_was_alive": True,
                    "native_tools": args.native_tools, "effective_policy": json.loads((root / "codex-thread.json").read_text())})
            print(json.dumps({"thread": peer.thread_id, "startup": "PASS"}), flush=True)
        finally:
            peer.close()
        return 0
    session = str(uuid.uuid4())
    base = ["/usr/bin/python3", str(SOURCE / "bridge.py"), "--runtime", str(runtime)]
    settings = {"hooks": {
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": shlex.join(base + ["hook"]), "timeout": 10}]}],
        "Stop": [{"hooks": [{"type": "command", "command": shlex.join(base + ["loop-hook"]),
                              "asyncRewake": True, "timeout": args.timeout + 10}]}]},
        "enabledPlugins": {}, "autoMemoryEnabled": False}
    atomic(root / "claude-settings.json", settings)
    command = shlex.join(base)
    begin = shlex.join(base + ["loop-begin", "--project", str(repo), "--task",
        "multiply(a,b) must return the mathematical product for positive, zero and negative integers.",
        "--file", "calculator.py", "--check",
        "python3 -B -c 'from calculator import multiply; assert multiply(3,4)==12; assert multiply(-2,3)==-6; assert multiply(0,9)==0'",
        "--read-only-review", "--max-rounds", "3", "--timeout", str(args.timeout)] + ([] if args.automatic_terminal else ["--manual-peer"]))
    prompt = f"""Run an explicitly authorized bridge communication smoke test in this isolated calculator fixture.
The current calculator intentionally has one small bug. For the FIRST round, leave it unchanged so the independent
reviewer can detect it. This exception applies only to this harmless fixture. Start with:
{begin}
Save the returned conversation_id. Write {repo / 'message.md'} in English explaining calculator.py's purpose,
that tests have not run, and requesting independent inspection against the supplied acceptance criteria.
Then run {command} loop-send CONVERSATION_ID --message-file {shlex.quote(str(repo / 'message.md'))}.
End your response immediately after sending. The test runner connects the paired Codex automatically.
When its async hook reply arrives, acknowledge it with the supplied loop-ack command, fix only calculator.py,
run the allowed bounded Python checks, write actual results in message.md, and loop-send with --in-reply-to 1.
On VERIFIED, acknowledge the final round and say the test completed. Do not send another review.
Do not commit, push, sync, install, inspect other projects, use subagents, or run other tools.
Do not poll or sleep in Bash. The Stop hook is responsible for feedback. All CLI calls use the exact runtime above.
"""
    atomic(root / "prompt.md", prompt.encode())
    env = {"HOME": str(Path.home()), "USER": os.environ.get("USER", ""), "PATH": config["path"],
           "LANG": "C.UTF-8", "TERM": "xterm-256color", "DISABLE_AUTOUPDATER": "1"}
    for key in ("WSL_INTEROP", "WSL_DISTRO_NAME"):
        if key in os.environ:
            env[key] = os.environ[key]
    argv = [config["claude"], "-p", "--input-format", "stream-json", "--output-format", "stream-json",
        "--verbose", "--include-hook-events", "--session-id", session,
        "--setting-sources", "", "--settings", str(root / "claude-settings.json"),
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands",
        "--tools", "Bash,Read,Write,Edit", "--permission-mode", "acceptEdits",
        "--allowedTools", f"Bash(/usr/bin/python3 {shlex.quote(str(SOURCE / 'bridge.py'))}:*)",
        f"Bash(/usr/bin/python3.12 {shlex.quote(str(SOURCE / 'bridge.py'))}:*)",
        "Bash(python3 -B -c:*)", "--effort", "low"]
    claude = peer = None
    identifier = None
    error = None
    started = now()
    deadline = time.monotonic() + args.timeout + 30
    logs = [open(root / n, "wb") for n in ("claude.jsonl", "claude.stderr.log", "peer.log")]
    try:
        claude = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=logs[0], stderr=logs[1],
            cwd=repo, env=env, start_new_session=True)
        claude.stdin.write((json.dumps({"type": "user", "session_id": session,
            "message": {"role": "user", "content": prompt}}) + "\n").encode())
        claude.stdin.flush()  # Keep the streaming session open so asyncRewake has a live recipient.
        from conversation import Conversations
        conversations = Conversations(Bridge(runtime))
        pointer = conversations.pointer(session)
        while time.monotonic() < deadline:
            if identifier is None and pointer.exists():
                identifier = json.loads(pointer.read_text())["conversation_id"]
                if not args.automatic_terminal:
                    peer = subprocess.Popen(base + ["loop-peer", identifier, "--headless"],
                        stdin=subprocess.DEVNULL, stdout=logs[2], stderr=logs[2], env=env,
                        cwd=repo, start_new_session=True)
                print(f"Conversation: {identifier}", flush=True)
            if identifier:
                state = conversations.read(identifier)
                if state["state"] == "COMPLETED" and state["acknowledged_round"] == state["round"]:
                    assert state["round"] == 2, state
                    first = json.loads((conversations.directory(identifier) / "round-001/result.json").read_text())
                    assert first["verdict"] == "NEEDS_FIXES", first
                    assert state["reply"]["verdict"] == "VERIFIED", state
                    break
                if state["state"] in ("FAILED", "BLOCKED", "INTERRUPTED", "CANCELLED", "LIMIT_REACHED", "TIMED_OUT"):
                    raise RuntimeError(json.dumps(state))
            if claude.poll() is not None:
                raise RuntimeError(f"Claude exited before feedback acknowledgement ({claude.returncode})")
            # A completed callback without an acknowledgement is a failure, not proof
            # of delivery. Do not spend the whole timeout on an idle permission denial.
            events = [json.loads(line) for line in (root / "claude.jsonl").read_text().splitlines()
                      if line.endswith("}")]
            results = [e for e in events if e.get("type") == "result"]
            if not identifier and results:
                raise RuntimeError("Claude completed without creating the conversation; see claude.jsonl")
            if identifier and state.get("wake_status") == "EMITTED" and len(results) >= state["round"] + 1:
                raise RuntimeError("Claude completed its feedback turn without acknowledging it; see claude.jsonl")
            time.sleep(.25)
        else:
            raise TimeoutError("Real conversation smoke deadline exceeded")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        for proc in (peer, claude):
            if proc:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        terminate_owned(descendants(proc.pid) + [process(proc.pid)])
                        proc.wait(timeout=5)
        for log in logs:
            log.close()
        result = {"started_at": started, "finished_at": now(), "result": "FAIL" if error else "PASS",
            "conversation_id": identifier, "claude_session_id": session, "error": error,
            "scope": "Isolated calculator fixture; real Claude asyncRewake and real Codex app-server",
            "runtime": str(runtime), "session_start_hooks": "Disabled using explicit setting sources",
            "console": "Automatic Windows Terminal + native Codex" if args.automatic_terminal else
                       "Headless streaming CLI transport; this run does not verify native TUI attachment"}
        atomic(root / "result.json", result)
        print(json.dumps(result, indent=2), flush=True)
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main())
