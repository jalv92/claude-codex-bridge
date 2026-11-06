"""Behavioral contract tests through the public CLI; no model or network calls."""
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from contracts import MANIFEST, REQUEST, RESULT, digest, validate
from runtime import alive, process, lock, now
from bridge import Bridge, codex_command


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bridge test spaces ")
        self.root = Path(self.temp.name)
        self.repo = self.root / "project ; $(touch NEVER)"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Bridge fixture")
        (self.repo / ".gitignore").write_text("data/\n")
        (self.repo / "example.py").write_text("value = 1\n")
        (self.repo / ".gitattributes").write_text("hidden.txt export-ignore\n")
        (self.repo / "hidden.txt").write_text("Exact Git blob must be reviewed.\n")
        self.git("add", ".gitignore", "example.py", ".gitattributes", "hidden.txt")
        self.git("commit", "-qm", "Initial fixture")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.runtime = self.root / "runtime with spaces"
        self.fake = self.root / "fake codex ; literal"
        shutil.copyfile(SOURCE / "tests/fake_codex.py", self.fake)
        self.fake.chmod(0o755)
        self.scenario("success")
        self.cli("configure", "--project", self.repo, "--codex", self.fake)

    def tearDown(self):
        for p in self.runtime.glob("jobs/*/state.json"):
            try:
                s = json.loads(p.read_text())
                from runtime import terminate_owned
                terminate_owned(s.get("children", []) + [s.get("worker"), s.get("codex_process")])
            except (OSError, ValueError):
                pass
        # Snapshots are deliberately read-only; restore fixture permissions for cleanup.
        for p in self.root.rglob("*"):
            if not p.is_symlink():
                p.chmod(0o700 if p.is_dir() else 0o600)
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["/usr/bin/git", "-C", str(self.repo), *args], text=True,
                                       stderr=subprocess.DEVNULL)

    def cli(self, *args, ok=True, input=None, env=None):
        e = {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        if env:
            e.update(env)
        r = subprocess.run(["/usr/bin/python3", str(SOURCE / "bridge.py"), "--runtime", str(self.runtime),
                            *map(str, args)], input=input, capture_output=True, text=True, env=e, timeout=20)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        else:
            self.assertNotEqual(r.returncode, 0)
        return json.loads(r.stdout) if r.stdout else r

    def scenario(self, mode):
        (self.root / "scenario").write_text(mode)

    def arm(self, task="fixture", session="session-1"):
        return self.cli("arm", "--project", self.repo, "--session", session,
                        "--session-cwd", self.repo, "--task", task)

    def prepared(self, timeout=10, artifacts=None):
        a = self.arm(task=str(time.time_ns()))
        job = a["job_id"]
        draft = Path(a["draft"])
        request = json.loads((draft / "request.json").read_text())
        prompt = "# Review fixture\n\n" + "\n".join(
            f"## {heading}\n{body}\n" for heading, body in [
                ("Objective", "Independently inspect the committed fixture."),
                ("Changed-file purposes", "example.py declares a test value."),
                ("Reproduction commands", "None authorized; static inspection only."),
                ("Actual test results", "No reproduction was run; static fixture construction succeeded."),
                ("Limitations", "No economic claims. Literal $(touch NEVER) `touch NEVER` ; are data."),
                ("Acceptance criteria", "Final source must define value = 1; independently inspect the evidence.")])
        path = draft / "prompt ; $(touch NEVER).md"
        path.write_text(prompt)
        request.update(prompt=str(path), timeout_seconds=timeout, artifacts=artifacts or [])
        request["scope"]["description"] = "Static inspection of the harmless fixture"
        (draft / "request.json").write_text(json.dumps(request))
        return job, draft, request

    def ready(self, **kwargs):
        job, draft, req = self.prepared(**kwargs)
        self.cli("submit", job)
        return job

    def state(self, job):
        return json.loads((self.runtime / "jobs" / job / "state.json").read_text())

    def wait(self, job, terminal=True):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            s = self.state(job)
            if terminal and s["state"] != "RUNNING":
                return s
            if not terminal and s.get("codex_process"):
                return s
            time.sleep(.05)
        self.fail(f"Job did not reach expected state: {self.state(job)}")

    def event(self, **kwargs):
        return json.dumps({"hook_event_name": "Stop", "session_id": "session-1",
                           "cwd": str(self.repo), "stop_hook_active": False, **kwargs})

    def test_no_readiness_wrong_identity_recursion_and_event(self):
        self.cli("hook", input=self.event())
        a = self.arm()
        self.cli("hook", input=self.event())
        self.assertEqual(self.state(a["job_id"])["state"], "ARMED")
        job = self.ready()
        for event in [self.event(session_id="other"), self.event(cwd=str(self.root)),
                      self.event(stop_hook_active=True), self.event(stop_hook_active="false"),
                      self.event(hook_event_name="SubagentStop"), self.event(agent_id="child"), "{broken"]:
            self.cli("hook", input=event)
        self.cli("hook", input=self.event(), env={"CLAUDE_CODEX_BRIDGE_CHILD": "1"})
        self.assertEqual(self.state(job)["state"], "READY")
        self.assertFalse((self.root / "launches.jsonl").exists())

    def test_identity_observer_auto_arm_without_real_claude(self):
        # A copied Python interpreter gives the harness a real executable named claude.
        # No Claude session or SessionStart hooks are started.
        interpreter = self.root / "claude"
        shutil.copyfile(sys.executable, interpreter)
        interpreter.chmod(0o755)
        harness = self.root / "session_harness.py"
        harness.write_text('''import json, subprocess, sys
base = ["/usr/bin/python3", sys.argv[1], "--runtime", sys.argv[2]]
event = {"hook_event_name":"UserPromptSubmit","session_id":sys.argv[4],"cwd":sys.argv[3]}
subprocess.run(base+["hook"], input=json.dumps(event), text=True, check=True)
subprocess.run(base+["arm","--project",sys.argv[3],"--task","auto-session"], check=True)
''')
        for identity in ("origin-one", "origin-two"):
            r = subprocess.run([str(interpreter), str(harness), str(SOURCE / "bridge.py"),
                str(self.runtime), str(self.repo), identity], capture_output=True, text=True,
                env={"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin"}, timeout=10)
            self.assertEqual(r.returncode, 0, r.stderr)
            job = json.loads(r.stdout)["job_id"]
            self.assertEqual(self.state(job)["session_id"], identity)
        self.cli("arm", "--project", self.repo, "--task", "no-parent", ok=False)

    def test_cancel_and_child_mutation_guard(self):
        a = self.arm()
        self.cli("cancel", a["job_id"])
        self.assertEqual(self.state(a["job_id"])["state"], "CANCELLED")
        self.cli("arm", "--project", self.repo, "--task", "forbidden", ok=False,
                 env={"CLAUDE_CODEX_BRIDGE_CHILD": "1"})

    def test_lifetime_lock_prevents_false_stale_from_other_pid_namespace(self):
        a = self.arm()
        bridge = Bridge(self.runtime)
        job = bridge.job(a["job_id"])
        identity = {**process(os.getpid()), "start": 0, "pid_namespace": "pid:[other]"}
        s = bridge.state(job)
        bridge.save(job, s, state="RUNNING", phase="REVIEWING", worker=identity, claimed_at=now())
        with lock(job / "worker.lock"):
            self.assertEqual(bridge.refresh(job, s)["state"], "RUNNING")
        self.assertEqual(bridge.refresh(job, s)["state"], "INTERRUPTED")
        self.cli("run", a["job_id"], "--recover", ok=False)

    def test_concurrent_stop_detached_worker_and_literal_inputs(self):
        self.scenario("slow")
        job = self.ready()
        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.cli("hook", input=self.event()), range(8)))
        self.assertLess(time.monotonic() - started, 2)
        s = self.wait(job)
        self.assertEqual(s["state"], "COMPLETED", s)
        self.assertEqual(s["verdict"], "VERIFIED")
        self.assertEqual(s["codex_session_id"], "fake-session-123")
        launches = (self.root / "launches.jsonl").read_text().splitlines()
        self.assertEqual(len(launches), 1)
        self.assertIn("$(touch NEVER)", json.loads(launches[0])["prompt"])
        self.assertFalse((self.repo / "NEVER").exists())
        self.assertTrue(Path(s["report"]).is_file())
        final = Path(s["attempt_dir"]) / "final"
        self.assertEqual((final / "hidden.txt").read_text(), "Exact Git blob must be reviewed.\n")
        self.cli("hook", input=self.event())
        self.cli("run", job, ok=False)

    def test_reject_invalid_incomplete_dirty_and_out_of_scope(self):
        job, draft, request = self.prepared()
        path = draft / "request.json"
        for change in [{"schema_version": 2}, {"timeout_seconds": True}, {"unexpected": "field"},
                       {"scope": {"description": "x"}}, {"final_commit": "main"},
                       {"base_commit": "0" * 40}, {"timeout_seconds": 7200}]:
            path.write_text(json.dumps({**request, **change}))
            self.cli("submit", job, ok=False)
        path.write_text(json.dumps(request))
        p = Path(request["prompt"])
        saved = p.read_text()
        p.write_text("## Objective\nIncomplete.")
        self.cli("submit", job, ok=False)
        p.write_text(saved)
        (self.repo / "example.py").write_text("uncommitted\n")
        self.cli("submit", job, ok=False)
        (self.repo / "example.py").write_text("value = 1\n")
        outside = self.root / "outside.txt"
        outside.write_text("outside")
        request["artifacts"] = [{"path": str(outside), "mode": "reference", "purpose": "Unlisted root"}]
        path.write_text(json.dumps(request))
        self.cli("submit", job, ok=False)
        self.assertEqual(self.state(job)["state"], "ARMED")

    def test_frozen_package_tamper_and_draft_independence(self):
        job = self.ready()
        (self.runtime / "jobs" / job / "draft/prompt.md").write_text("draft changes are irrelevant")
        self.cli("submit", job, ok=False)
        package = self.runtime / "jobs" / job / "package"
        (package / "prompt.md").chmod(0o600)
        (package / "prompt.md").write_text("tampered")
        self.cli("hook", input=self.event())
        self.assertEqual(self.state(job)["state"], "FAILED")
        self.assertFalse((self.root / "launches.jsonl").exists())
        job2 = self.ready()
        manifest = self.runtime / "jobs" / job2 / "package/manifest.json"
        manifest.chmod(0o600)
        manifest.write_text("{}")
        self.cli("hook", input=self.event())
        self.assertEqual(self.state(job2)["state"], "FAILED")

    def test_artifacts_copy_and_reference_changes(self):
        data = self.repo / "data"
        data.mkdir()
        evidence = data / "evidence ; $(touch NEVER).txt"
        evidence.write_text("frozen evidence")
        job = self.ready(artifacts=[{"path": str(evidence), "mode": "copy", "purpose": "Small evidence"}])
        evidence.write_text("changed source; frozen copy remains valid")
        self.cli("run", job, "--wait")
        self.assertEqual(self.state(job)["state"], "COMPLETED")
        job = self.ready(artifacts=[{"path": str(evidence), "mode": "reference", "purpose": "Large data"}])
        evidence.write_text("changed reference")
        self.cli("hook", input=self.event())
        self.assertEqual(self.state(job)["state"], "FAILED")
        self.assertIn("changed", self.state(job)["error"])

    def test_changed_reference_during_review_becomes_not_verifiable(self):
        self.scenario("slow")
        p = self.repo / "data"
        p.mkdir()
        f = p / "reference.txt"
        f.write_text("initial evidence")
        job = self.ready(artifacts=[{"path": str(f), "mode": "reference", "purpose": "Reference evidence"}])
        self.cli("run", job)
        self.wait(job, terminal=False)
        f.write_text("updated evidence")
        s = self.wait(job)
        self.assertEqual(s["state"], "COMPLETED", s)
        self.assertEqual(s["verdict"], "NOT_VERIFIABLE")

    def test_output_semantics_and_launch_failure(self):
        for mode, state, verdict in [("success", "COMPLETED", "VERIFIED"),
                                     ("fixes", "COMPLETED", "NEEDS_FIXES"),
                                     ("malformed", "FAILED", None), ("missing", "FAILED", None),
                                     ("badverdict", "FAILED", None), ("nonzero", "FAILED", None)]:
            with self.subTest(mode=mode):
                self.scenario(mode)
                job = self.ready()
                self.cli("run", job, "--wait")
                s = self.state(job)
                self.assertEqual((s["state"], s.get("verdict")), (state, verdict), s)
                self.assertEqual(s["worker_exit_code"], 0 if state == "COMPLETED" else 1)
                if mode == "nonzero":
                    self.assertEqual(s["exit_code"], 7)
        job = self.ready()
        self.fake.unlink()
        self.cli("run", job, "--wait")
        self.assertEqual(self.state(job)["state"], "FAILED")

    def test_timeout_kills_own_detached_descendants(self):
        self.scenario("timeout")
        unrelated = subprocess.Popen(["/usr/bin/python3", "-c", "import time; time.sleep(60)"])
        try:
            job = self.ready(timeout=2)
            self.cli("run", job, "--wait")
            s = self.state(job)
            self.assertEqual(s["state"], "TIMED_OUT", s)
            self.assertIsNone(unrelated.poll())
            child = int((self.root / "child.pid").read_text())
            self.assertIsNone(process(child))
        finally:
            unrelated.kill()
            unrelated.wait()

    def test_interrupted_worker_explicit_recovery_preserves_attempt(self):
        self.scenario("interrupt")
        job = self.ready()
        self.cli("run", job)
        s = self.wait(job, terminal=False)
        time.sleep(.3)
        old_codex = s["codex_process"]
        os.kill(s["worker"]["pid"], signal.SIGKILL)
        time.sleep(.1)
        self.assertEqual(self.cli("status", job)[0]["state"], "INTERRUPTED")
        self.cli("hook", input=self.event())
        self.cli("run", job, ok=False)
        self.scenario("success")
        self.cli("run", job, "--recover", "--wait")
        s = self.state(job)
        self.assertEqual((s["state"], s["attempt"]), ("COMPLETED", 2), s)
        self.assertFalse(alive(old_codex))
        self.assertTrue((self.runtime / "jobs" / job / "attempt-001/codex.events.jsonl").exists())

    def test_install_uninstall_preserves_other_settings(self):
        settings = self.root / "settings with spaces.json"
        original = {"env": {"UNRELATED": "kept"}, "hooks": {
            "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "echo existing"}]}],
            "SessionStart": [{"hooks": [{"type": "command", "command": "echo auto-sync"}]}]}}
        settings.write_text(json.dumps(original))
        self.cli("install-hook", "--settings", settings)
        first = json.loads(settings.read_text())
        self.cli("install-hook", "--settings", settings)
        self.assertEqual(json.loads(settings.read_text()), first)
        hook = first["hooks"]["Stop"][-1]["hooks"][0]["command"]
        # Exercise the exact installed command under a noninteractive minimal PATH.
        r = subprocess.run(["/bin/sh", "-c", hook], input=self.event(), text=True, capture_output=True,
                           env={"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.cli("uninstall-hook", "--settings", settings)
        self.assertEqual(json.loads(settings.read_text()), original)
        self.cli("uninstall-hook", "--settings", settings)
        self.assertEqual(json.loads(settings.read_text()), original)

    def test_committed_snapshot_does_not_follow_live_branch(self):
        job = self.ready()
        (self.repo / "example.py").write_text("value = 999\n")
        self.git("add", "example.py")
        self.git("commit", "-qm", "Concurrent fixture change")
        self.cli("run", job, "--wait")
        s = self.state(job)
        self.assertEqual(s["state"], "COMPLETED", s)
        self.assertEqual((Path(s["attempt_dir"]) / "final/example.py").read_text(), "value = 1\n")
        self.assertEqual((self.repo / "example.py").read_text(), "value = 999\n")

    def test_schema_files_match_validators_and_scratch_scope(self):
        for name, schema in [("request", REQUEST), ("manifest", MANIFEST), ("result", RESULT)]:
            doc = json.loads((SOURCE / f"schemas/{name}.schema.json").read_text())
            doc.pop("$schema")
            self.assertEqual(doc, schema)
        job = self.ready()
        b = Bridge(self.runtime)
        m = b.package(b.job(job), b.state(b.job(job)))
        m["scope"]["scratch_write"] = True
        cmd = codex_command(b.config(), self.root / "attempt", m)
        self.assertNotIn("--sandbox", cmd)
        self.assertIn('default_permissions="bridge_review"', cmd)
        self.assertTrue(any('extends = ":read-only"' in arg for arg in cmd))
        self.assertFalse(any("danger-full-access" in a for a in cmd))
        self.assertIn('approval_policy="never"', cmd)
        self.assertIn('allow_login_shell=false', cmd)


if __name__ == "__main__":
    unittest.main()
