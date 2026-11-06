"""Local OS sandbox checks, with no model calls or credentials printed."""
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import Bridge, DEFAULT_RUNTIME, codex_command, child_env
from runtime import atomic

bridge = Bridge(DEFAULT_RUNTIME)
config = bridge.config()
root = bridge.root / "sandbox-probe"
root.mkdir(exist_ok=True)
(root / "final").mkdir(exist_ok=True)
(root / "scratch").mkdir(exist_ok=True)
check = root / "probe.py"
check.write_text('''import errno, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
expected_scratch = sys.argv[2] == "true"
result = {}
for label, parent in [("source", root / "final"), ("scratch", root / "scratch")]:
    try:
        (parent / "probe.txt").write_text("sandbox probe")
        result[label] = True
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EROFS, errno.EPERM):
            raise
        result[label] = False
assert result == {"source": False, "scratch": expected_scratch}, result
print(json.dumps(result))
''')
results = []
for scratch in (False, True):
    command = codex_command(config, root, {"scope": {"scratch_write": scratch}})
    overrides = []
    for i, arg in enumerate(command[:-1]):
        if arg in ("-c", "--disable", "--enable"):
            overrides += [arg, command[i + 1]]
    profile = "bridge_review" if scratch else ":read-only"
    argv = [config["codex"], *overrides, "sandbox", "-P", profile,
            "-C", str(root / "final"), "--", "/usr/bin/python3", str(check), str(root),
            str(scratch).lower()]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=20, env=child_env(config))
    results.append({"scratch_write": scratch, "exit_code": r.returncode,
                    "stdout": r.stdout, "stderr": r.stderr})
atomic(root / "results.json", results)
print(json.dumps(results, indent=2))
sys.exit(0 if all(r["exit_code"] == 0 for r in results) else 1)
