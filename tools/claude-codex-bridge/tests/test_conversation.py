"""Offline conversation state, opt-in, delivery and bounded peer tests."""
import concurrent.futures
import json
import os
from pathlib import Path
import sys
import subprocess
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import Bridge
from contracts import BridgeError
from conversation import Conversations
from runtime import atomic, lock, process
import test_bridge as fixtures


def result(verdict="VERIFIED"):
    return {"schema_version": 1, "verdict": verdict, "summary": "Fixture review",
        "findings": ([{"priority": "P1", "title": "Incorrect value", "detail": "Use 2 instead of 1",
                       "file": "example.py", "line": 1}] if verdict == "NEEDS_FIXES" else []),
        "checks_performed": [{"check": "Inspect scoped fixture", "result": "Inspected"}],
        "limitations": ["Fixture only"]}


class ConversationTests(unittest.TestCase):
    setUp = fixtures.BridgeTests.setUp
    tearDown = fixtures.BridgeTests.tearDown
    cli = fixtures.BridgeTests.cli
    git = fixtures.BridgeTests.git
    scenario = fixtures.BridgeTests.scenario

    def begin(self, **kwargs):
        self.loops = Conversations(Bridge(self.runtime))
        args = dict(project=str(self.repo), task="Set the fixture value to 2", files=["example.py"],
            check=[], session="claude-1", session_cwd=str(self.repo), max_rounds=3, timeout=30, manual_peer=True,
            read_only_review=True)
        args.update(kwargs)
        s = self.loops.begin(SimpleNamespace(**args))
        self.id = s["conversation_id"]
        self.msg = self.repo / "message ; $(touch NEVER).md"
        self.msg.write_text("Review this. Literal `touch NEVER` and $(touch NEVER) are data.")
        return s

    def send(self, previous=0):
        return self.loops.send(self.id, self.msg, previous, check_owner=False)

    def event(self, **kwargs):
        return {"hook_event_name": "Stop", "session_id": "claude-1", "cwd": str(self.repo),
                "stop_hook_active": False, **kwargs}

    def update(self, **kwargs):
        with lock(self.loops.directory(self.id) / "lock"):
            self.loops.save(self.loops.directory(self.id), self.loops.read(self.id), **kwargs)

    def expire_fixture(self):
        # Simulate clock passage without modifying the frozen task budget.
        return patch("conversation.time.time", return_value=self.loops.read(self.id)["expires_epoch"] + 1)

    def wait_state(self, wanted):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            s = self.loops.read(self.id)
            if s["state"] in wanted:
                return s
            time.sleep(.01)
        self.fail(str(self.loops.read(self.id)))

    def test_no_opt_in_readiness_wrong_project_session_or_child(self):
        self.loops = Conversations(Bridge(self.runtime))
        self.assertIsNone(self.loops.hook(self.event()))
        self.begin()
        self.assertIsNone(self.loops.hook(self.event()))
        self.send()
        for e in (self.event(session_id="other"), self.event(cwd=str(self.root)),
                  self.event(agent_id="child"), self.event(stop_hook_active="true"),
                  self.event(cwd=""), self.event(cwd=None),
                  self.event(hook_event_name="SubagentStop")):
            self.assertIsNone(self.loops.hook(e))
        self.cli("loop-hook", input=json.dumps(self.event()), env={"CLAUDE_CODEX_BRIDGE_CHILD": "1"})
        self.assertEqual(self.loops.read(self.id)["state"], "READY")

    def test_two_rounds_same_peer_concurrent_stop_single_delivery_and_ack(self):
        self.begin()
        self.send()
        launches = []
        class FakePeer:
            thread_id = "one-thread"
            ui = None
            count = 0
            def __init__(_self, *args):
                launches.append(args)
            def review(_self, prompt, cwd, deadline, cancelled):
                _self.count += 1
                self.assertIn("$(touch NEVER)", prompt)
                self.assertTrue((cwd / "example.py").is_file())
                return result("NEEDS_FIXES" if _self.count == 1 else "VERIFIED"), str(_self.count)
            def close(_self):
                pass
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            peer = pool.submit(self.loops.peer, self.id, False, FakePeer)
            hooks = [pool.submit(self.loops.hook, self.event()) for _ in range(3)]
            replies = [p.result(timeout=5) for p in hooks]
            self.assertEqual(sum(x is not None for x in replies), 1)
            self.assertEqual(self.loops.read(self.id)["state"], "AWAITING_CLAUDE")
            self.assertEqual(self.loops.read(self.id)["wake_status"], "EMITTED")
            feedback = next(x for x in replies if x is not None)
            self.loops.user_prompt(self.event(hook_event_name="UserPromptSubmit", prompt=
                '<task-notification>\n<summary>Stop hook feedback</summary>\n</task-notification>\n'
                '<system-reminder>\nStop hook blocking error from command "Stop": ' + feedback + '\n\n</system-reminder>'))
            self.assertEqual(self.loops.read(self.id)["state"], "AWAITING_CLAUDE")
            self.loops.ack(self.id, 1, check_owner=False)
            (self.repo / "example.py").write_text("value = 2\n")
            with self.assertRaises(BridgeError):
                self.send(0)
            self.send(1)
            # A fresh explicit message may be sent on a hook-driven continuation.
            response = pool.submit(self.loops.hook, self.event(stop_hook_active=True)).result(timeout=5)
            self.assertIn("conversation is closed", response)
            self.loops.ack(self.id, 2, check_owner=False)
            final = peer.result(timeout=5)
        self.assertEqual(final["state"], "COMPLETED")
        self.assertEqual(final["codex_thread_id"], "one-thread")
        self.assertEqual(self.loops.read(self.id)["acknowledged_round"], 2)
        self.assertEqual(len(launches), 1)
        self.assertFalse((self.repo / "NEVER").exists())
        self.assertTrue(Path(final["report"]).exists())
        self.assertIsNone(self.loops.hook(self.event()))

    def test_new_human_prompt_cancels_only_matching_pair(self):
        self.begin(); self.send()
        self.loops.user_prompt(self.event(session_id="other"))
        self.assertEqual(self.loops.read(self.id)["state"], "READY")
        self.cli("hook", input=json.dumps(self.event(hook_event_name="UserPromptSubmit", cwd=str(self.root))))
        self.assertEqual(self.loops.read(self.id)["state"], "CANCELLED")

        self.begin(); self.send(); self.update(state="REVIEWING")
        self.loops.reply(self.id, result("NEEDS_FIXES"), "turn")
        self.update(state="AWAITING_CLAUDE", delivered_round=1)
        (self.repo / "example.py").write_text("changed after delivery")
        with self.assertRaises(BridgeError):
            self.loops.ack(self.id, 1, check_owner=False)
        self.assertEqual(self.loops.read(self.id)["state"], "BLOCKED")
        self.assertIsNone(self.loops.hook(self.event()))

    def test_explicit_owner_duplicate_session_scope_and_tamper(self):
        self.begin()
        with self.assertRaises(BridgeError):
            self.loops.send(self.id, self.msg)
        with self.assertRaises(BridgeError):
            self.begin()
        self.send()
        rd = self.loops.directory(self.id) / "round-001"
        p = rd / "target/example.py"
        p.chmod(0o600); p.write_text("changed")
        self.assertIsNone(self.loops.hook(self.event()))
        self.assertEqual(self.loops.read(self.id)["state"], "FAILED")
        for files in (["../other"], [".env"], [".claude"]):
            with self.assertRaises(BridgeError):
                self.begin(files=files)
        self.begin(files=["data"])
        (self.repo / "data").mkdir()
        (self.repo / "data/large").write_text("ignored")
        with self.assertRaises(BridgeError):
            self.send()

    def test_drift_cancellation_malformed_result_and_round_limit(self):
        self.begin(max_rounds=1); self.send(); self.update(state="REVIEWING")
        with self.assertRaises(BridgeError):
            self.loops.reply(self.id, {"verdict": "VERIFIED"}, "turn")
        self.loops.reply(self.id, result("NEEDS_FIXES"), "turn")
        self.assertEqual(self.loops.read(self.id)["state"], "LIMIT_REACHED")
        self.begin(); self.send(); self.update(state="REVIEWING")
        (self.repo / "example.py").write_text("drift")
        self.assertFalse(self.loops.reply(self.id, result(), "turn"))
        self.assertEqual(self.loops.read(self.id)["state"], "BLOCKED")
        self.begin(); self.send(); self.update(state="REVIEWING")
        self.loops.stop(self.id, "human stop", "CANCELLED")
        self.assertFalse(self.loops.reply(self.id, result(), "turn"))
        self.assertEqual(self.loops.read(self.id)["state"], "CANCELLED")

    def test_deadline_interrupt_and_no_silent_peer_replacement(self):
        self.begin(); self.send()
        with self.expire_fixture():
            response = self.loops.hook(self.event())
        self.assertIn("TIMED_OUT", response)
        self.assertEqual(self.loops.read(self.id)["state"], "TIMED_OUT")
        self.begin(); self.send(); self.update(state="QUEUED")
        class BrokenPeer:
            thread_id = "broken-thread"
            ui = None
            def __init__(self, *args): pass
            def review(self, *args): raise InterruptedError("broken pipe")
            def close(self): pass
        end = self.loops.peer(self.id, False, BrokenPeer)
        self.assertEqual(end["state"], "INTERRUPTED")
        with self.assertRaises(BridgeError):
            self.loops.peer(self.id, False, BrokenPeer)

    def test_installation_async_rewake_and_uninstall_preservation(self):
        path = self.root / "settings.json"
        before = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing-hook"}]}]}}
        atomic(path, before)
        b = Bridge(self.runtime)
        b.install(path); b.install(path)
        hooks = [h for group in json.loads(path.read_text())["hooks"]["Stop"] for h in group["hooks"]]
        self.assertEqual(len(hooks), 3)
        self.assertEqual(sum(h.get("asyncRewake") is True for h in hooks), 1)
        b.install(path, True); b.install(path, True)
        self.assertEqual(json.loads(path.read_text()), before)

    def test_stale_peer_and_versioned_round_contract(self):
        from conversation_contracts import ROUND, validate_round
        from test_bridge import SOURCE
        self.begin(); self.send()
        s = self.loops.read(self.id)
        _, request = self.loops.request(s)
        validate_round(request)
        schema = json.loads((SOURCE / "schemas/conversation-round.schema.json").read_text())
        schema.pop("$schema")
        self.assertEqual(schema, ROUND)
        self.update(state="REVIEWING", codex_thread_id="lost-thread")
        with lock(self.loops.directory(self.id) / "peer.lock"):
            self.assertEqual(self.loops.status(self.id)["state"], "REVIEWING")
        self.assertEqual(self.loops.status(self.id)["state"], "INTERRUPTED")
        self.begin()
        self.update(task="Changed criteria")
        with self.assertRaises(BridgeError):
            self.loops.read(self.id)

    def test_cancel_cleans_only_recorded_orphan_processes(self):
        self.begin()
        own = subprocess.Popen(["/usr/bin/sleep", "10"])
        unrelated = subprocess.Popen(["/usr/bin/sleep", "10"])
        try:
            atomic(self.loops.directory(self.id) / "server-process.json", {"server": process(own.pid), "children": []})
            self.update(state="REVIEWING", peer_phase="CONNECTED", codex_thread_id="lost")
            self.loops.cancel(self.id)
            own.wait(timeout=3)
            self.assertIsNotNone(own.returncode)
            self.assertIsNone(unrelated.poll())
            self.assertEqual(self.loops.read(self.id)["state"], "CANCELLED")
        finally:
            if own.poll() is None: own.terminate(); own.wait()
            unrelated.terminate(); unrelated.wait()

    def test_native_edit_returned_and_acknowledged_without_stale_input_failure(self):
        self.begin(read_only_review=False)
        self.send()
        class EditingPeer:
            thread_id = "native-thread"
            ui = None
            def __init__(_self, config, directory, project, instructions, visible):
                self.assertEqual(config["conversation_mode"], "native")
                self.assertIn("tools and integrations configured", instructions)
            def review(_self, prompt, cwd, deadline, cancelled):
                self.assertEqual(cwd, self.repo)
                (cwd / "example.py").write_text("value = 2\n")
                return result(), "edit-turn"
            def close(_self): pass
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            peer = pool.submit(self.loops.peer, self.id, True, EditingPeer)
            feedback = pool.submit(self.loops.hook, self.event()).result(timeout=5)
            self.assertEqual(peer.result(timeout=5)["state"], "COMPLETED")
        self.assertIn("changes_during_codex_turn", feedback)
        self.loops.ack(self.id, 1, check_owner=False)
        state = self.loops.read(self.id)
        rd, request = self.loops.request(state)
        self.assertNotEqual(request["files"], self.loops.expected_files(state))
        self.assertEqual((rd / "output/example.py").read_text(), "value = 2\n")
        (self.repo / "example.py").write_text("later unrelated edit")
        with self.assertRaises(BridgeError):
            self.loops.ack(self.id, 1, check_owner=False)

    def test_native_scope_includes_assets_in_plain_project_and_mode_is_frozen(self):
        project = self.root / "plain site"
        project.mkdir()
        (project / "index.html").write_text("<html></html>")
        self.begin(project=str(project), session_cwd=str(project), create_project=True,
                   files=["index.html", "assets"], read_only_review=False)
        self.send(); self.update(state="REVIEWING")
        (project / "assets").mkdir()
        (project / "assets/car.svg").write_text("<svg/>")
        self.assertTrue(self.loops.reply(self.id, result(), "assets-turn"))
        state = self.loops.read(self.id)
        self.assertIn("assets/car.svg", self.loops.expected_files(state))
        self.update(codex_mode="read-only")
        with self.assertRaises(BridgeError):
            self.loops.read(self.id)

    def test_native_blocks_source_drift_before_model_turn(self):
        self.begin(read_only_review=False); self.send(); self.update(state="QUEUED")
        (self.repo / "example.py").write_text("concurrent edit before dispatch")
        class NeverReview:
            thread_id = "native-thread"
            ui = None
            def __init__(self, *args): pass
            def review(self, *args): raise AssertionError("Must not run a model turn")
            def close(self): pass
        state = self.loops.peer(self.id, True, NeverReview)
        self.assertEqual(state["state"], "FAILED")
        self.assertIn("changed since publication", state["error"])

    def test_native_project_lock_blocks_a_second_peer_without_launching_it(self):
        import hashlib
        self.begin(read_only_review=False); self.send(); self.update(state="QUEUED")
        locks = self.loops.root / "project-locks"
        locks.mkdir()
        project_lock = locks / (hashlib.sha256(str(self.repo).encode()).hexdigest() + ".lock")
        with lock(project_lock):
            with self.assertRaisesRegex(BridgeError, "Another native-tool conversation"):
                self.loops.peer(self.id, True, lambda *args: self.fail("Must not launch"))
        self.assertEqual(self.loops.read(self.id)["state"], "BLOCKED")

    def propose_round(self, verdict="VERIFIED"):
        """Run one proposal turn headless; Codex edits only its scratch copy."""
        self.begin(read_only_review=False, propose=True)
        self.send()
        work = self.loops.directory(self.id) / "work"
        class ProposingPeer:
            thread_id = "propose-thread"
            ui = None
            def __init__(_self, config, directory, project, instructions, visible):
                self.assertEqual(config["conversation_mode"], "propose")
                self.assertEqual(project, work)
                self.assertIn("proposal", instructions)
            def review(_self, prompt, cwd, deadline, cancelled):
                self.assertEqual(cwd, work)
                (cwd / "example.py").write_text("value = 2\n")
                (cwd / "out-of-scope.txt").write_text("never proposed")
                return result(verdict), "propose-turn"
            def close(_self): pass
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.addCleanup(pool.shutdown, wait=True, cancel_futures=True)
        peer = pool.submit(self.loops.peer, self.id, False, ProposingPeer)
        hook = pool.submit(self.loops.hook, self.event())
        self.wait_state({"AWAITING_HUMAN"})
        return peer, hook

    def test_propose_waits_for_human_and_applies_only_the_scoped_diff(self):
        peer, hook = self.propose_round()
        self.assertEqual((self.repo / "example.py").read_text(), "value = 1\n")
        diff = self.loops.diff(self.id)
        self.assertIn("-value = 1", diff); self.assertIn("+value = 2", diff)
        self.assertNotIn("out-of-scope", diff)
        self.assertFalse(hook.done())  # Claude is not woken before the human decides.
        with patch("conversation.claude_parent", return_value={"pid": 1}):
            with self.assertRaisesRegex(BridgeError, "human"):
                self.loops.decide(self.id, apply=True)
        with patch("conversation.claude_parent", return_value=None):
            self.loops.decide(self.id, apply=True)
        self.assertEqual((self.repo / "example.py").read_text(), "value = 2\n")
        feedback = hook.result(timeout=5)
        self.assertEqual(peer.result(timeout=5)["state"], "COMPLETED")
        self.assertIn("APPLIED", feedback)
        self.loops.ack(self.id, 1, check_owner=False)

    def test_propose_reject_keeps_project_and_tells_claude(self):
        peer, hook = self.propose_round()  # VERIFIED describes the diff, so rejecting must not complete
        with patch("conversation.claude_parent", return_value=None):
            self.loops.decide(self.id, apply=False, reason="Too broad")
        self.assertEqual((self.repo / "example.py").read_text(), "value = 1\n")
        feedback = hook.result(timeout=5)
        self.assertIn("REJECTED", feedback)
        self.assertEqual(self.loops.read(self.id)["state"], "AWAITING_CLAUDE")
        self.loops.ack(self.id, 1, check_owner=False)
        self.loops.cancel(self.id)
        peer.result(timeout=5)

    def test_propose_apply_refuses_a_project_that_changed(self):
        peer, hook = self.propose_round()
        (self.repo / "example.py").write_text("someone else edited this\n")
        with patch("conversation.claude_parent", return_value=None):
            with self.assertRaisesRegex(BridgeError, "changed"):
                self.loops.decide(self.id, apply=True)
        self.assertEqual((self.repo / "example.py").read_text(), "someone else edited this\n")
        self.assertEqual(self.loops.read(self.id)["state"], "BLOCKED")
        hook.result(timeout=5); peer.result(timeout=5)

    def test_propose_scratch_inventory_keeps_known_files_and_discards_junk(self):
        self.begin(read_only_review=False, propose=True, files=["example.py", "data"])
        work = self.root / "scratch"
        (work / "data/__pycache__").mkdir(parents=True)
        (work / "example.py").write_text("value = 2\n")
        (work / "data/tracked.bin").write_bytes(b"\1")   # in the snapshot although data/ is ignored
        (work / "data/cache.bin").write_bytes(b"\0")     # new and ignored by the live project
        (work / "data/__pycache__/x.pyc").write_bytes(b"\0")
        (work / "data/link").symlink_to("/etc/hostname")
        before = {"example.py": None, "data/tracked.bin": {"sha256": "old", "size": 1}}
        files, discarded = self.loops.scratch_files(self.loops.read(self.id), work, before)
        self.assertEqual(set(files), {"example.py", "data/tracked.bin"})
        self.assertEqual(discarded, ["data/__pycache__/x.pyc", "data/cache.bin", "data/link"])

    def test_propose_refuses_file_directory_swaps(self):
        self.begin(read_only_review=False, propose=True, files=["d", "a.py"])
        before = {"d/x": {"sha256": "x", "size": 1}, "a.py": {"sha256": "a", "size": 1}}
        for make in (lambda w: (w / "d").write_text("now a file"),
                     lambda w: ((w / "a.py").mkdir(), (w / "a.py/x").write_text("now a dir"))):
            work = self.root / f"swap-{id(make)}"
            work.mkdir()
            make(work)
            with self.assertRaisesRegex(BridgeError, "swapped a file and a directory"):
                self.loops.scratch_files(self.loops.read(self.id), work, before)

    def test_propose_scratch_copy_keeps_executable_bit(self):
        self.begin(read_only_review=False, propose=True)
        (self.repo / "example.py").chmod(0o755)
        self.send()
        s = self.loops.read(self.id)
        rd, request = self.loops.request(s)
        work = self.loops.prepare_work(self.loops.directory(self.id), rd, request, s["project"])
        self.assertTrue(os.access(work / "example.py", os.X_OK | os.W_OK))

    def test_propose_apply_waits_for_a_native_editor_on_the_same_project(self):
        peer, hook = self.propose_round()
        with self.loops.project_lock(str(self.repo)), patch("conversation.claude_parent", return_value=None):
            with self.assertRaisesRegex(BridgeError, "native-tool conversation"):
                self.loops.decide(self.id, apply=True)
        self.assertEqual((self.repo / "example.py").read_text(), "value = 1\n")
        self.loops.cancel(self.id); hook.result(timeout=5); peer.result(timeout=5)

    def test_propose_cli_requires_an_interactive_confirmation(self):
        peer, hook = self.propose_round()
        r = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "bridge.py"),
                            "--runtime", str(self.runtime), "loop-apply", self.id],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("interactive terminal", r.stderr)
        self.assertEqual((self.repo / "example.py").read_text(), "value = 1\n")
        self.loops.cancel(self.id); hook.result(timeout=5); peer.result(timeout=5)

    def test_propose_and_read_only_are_exclusive(self):
        with self.assertRaisesRegex(BridgeError, "propose"):
            self.begin(read_only_review=True, propose=True)

    def test_legacy_contract_stays_read_only(self):
        from conversation import IMMUTABLE
        from contracts import digest
        self.begin()
        directory = self.loops.directory(self.id)
        s = self.loops.read(self.id)
        del s["codex_mode"]
        atomic(directory / "contract.json", {k: s[k] for k in IMMUTABLE})
        s["contract_sha256"] = digest(directory / "contract.json")
        self.loops.save(directory, s)
        self.send(); self.update(state="REVIEWING")
        (self.repo / "example.py").write_text("changed")
        self.assertFalse(self.loops.reply(self.id, result(), "turn"))
        self.assertEqual(self.loops.read(self.id)["state"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
