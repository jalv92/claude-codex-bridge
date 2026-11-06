#!/usr/bin/python3
"""Deterministic executable fixture. Scenario is selected beside this script."""
import json
import os
import pathlib
import subprocess
import sys
import time

root = pathlib.Path(__file__).resolve().parent
args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.167.0-fake")
elif args == ["exec", "--help"]:
    print("--ignore-user-config --ignore-rules --output-schema --json --output-last-message --ephemeral --skip-git-repo-check")
elif args == ["features", "list"]:
    for name in ("apps plugins hooks multi_agent multi_agent_v2 browser_use browser_use_external computer_use "
                 "image_generation shell_snapshot memories skill_search skill_mcp_dependency_install remote_plugin daemon_auto_start skip_host_skill_discovery").split():
        print(name, "stable true")
elif args == ["login", "status"]:
    print("Logged in using fake authentication")
else:
    prompt = sys.stdin.read()
    mode = (root / "scenario").read_text().strip()
    with (root / "launches.jsonl").open("a") as f:
        f.write(json.dumps({"pid": os.getpid(), "args": args, "prompt": prompt,
                            "path": os.environ["PATH"], "child_guard": os.getenv("CLAUDE_CODEX_BRIDGE_CHILD")}) + "\n")
    print(json.dumps({"type": "thread.started", "thread_id": "fake-session-123"}), flush=True)
    if mode in ("timeout", "interrupt"):
        child = subprocess.Popen(["/usr/bin/python3", "-c", "import time; time.sleep(120)"], start_new_session=True)
        (root / "child.pid").write_text(str(child.pid))
        time.sleep(120)
    elif mode == "slow":
        time.sleep(1)
    elif mode == "nonzero":
        sys.exit(7)
    output = pathlib.Path(args[args.index("--output-last-message") + 1])
    if mode == "missing":
        sys.exit(0)
    if mode == "malformed":
        output.write_text('{"verdict":"VERIFIED"}')
    else:
        verdict = "NEEDS_FIXES" if mode == "fixes" else "VERIFIED"
        result = {"schema_version": 1, "verdict": verdict, "summary": "Fixture review completed.",
                  "findings": [], "checks_performed": [{"check": "Read fixture", "result": "Inspected final snapshot"}],
                  "limitations": []}
        if mode == "fixes":
            result["findings"] = [{"priority": "P1", "title": "Fixture finding", "detail": "Repair the fixture.",
                                   "file": "example.py", "line": 1}]
        if mode == "badverdict":
            result["verdict"] = "VERIFIED"
            result["findings"] = [{"priority": "P1", "title": "Contradiction", "detail": "Unresolved finding.",
                                   "file": "example.py", "line": 1}]
        output.write_text(json.dumps(result))
