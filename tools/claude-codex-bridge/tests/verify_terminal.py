"""Real Windows Terminal/WSL launch proof; no model calls and no Claude session."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from bridge import Bridge, DEFAULT_RUNTIME
from runtime import atomic, now, process
from terminal import spawn


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--child")
    args = p.parse_args()
    if args.child:
        proof = {"result": "PASS", "at": now(), "process": process(os.getpid()),
            "is_tty": sys.stdin.isatty(), "distribution": os.environ.get("WSL_DISTRO_NAME")}
        atomic(Path(args.child), proof)
        print("Bridge terminal launch test passed. This is a real new WSL terminal.\n"
              "No model is running. This test window closes in 12 seconds.", flush=True)
        time.sleep(12)
        return
    bridge = Bridge(DEFAULT_RUNTIME)
    directory = bridge.root / "terminal-probes" / uuid.uuid4().hex
    directory.mkdir(parents=True)
    proof = directory / "proof ; $(literal).json"
    result = spawn(bridge.config()["terminal"], ["/usr/bin/python3", str(Path(__file__).resolve()),
        "--child", str(proof)], directory / "launcher.log")
    end = time.monotonic() + 25
    while not proof.exists() and time.monotonic() < end:
        time.sleep(.1)
    if not proof.exists():
        raise RuntimeError(f"The window did not acknowledge startup; inspect {directory}")
    result.update(json.loads(proof.read_text()))
    assert result["is_tty"], result
    atomic(directory / "result.json", result)
    print(json.dumps({**result, "evidence": str(directory)}, indent=2))


if __name__ == "__main__":
    main()
