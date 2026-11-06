"""Automatic window dispatch, directory changes, and new non-Git projects."""
import base64
import concurrent.futures
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import test_conversation as fixture
from bridge import Bridge
from contracts import BridgeError
from runtime import atomic, now
from terminal import command, open_peer


class TerminalTests(unittest.TestCase):
    setUp = fixture.ConversationTests.setUp
    tearDown = fixture.ConversationTests.tearDown
    cli = fixture.ConversationTests.cli
    git = fixture.ConversationTests.git
    scenario = fixture.ConversationTests.scenario
    begin = fixture.ConversationTests.begin
    send = fixture.ConversationTests.send
    update = fixture.ConversationTests.update
    event = fixture.ConversationTests.event
    wait_state = fixture.ConversationTests.wait_state

    def terminal_config(self):
        b = Bridge(self.runtime)
        c = b.config()
        c["terminal"] = {"kind": "windows-terminal", "executable": "fake-wt",
                         "distribution": "Ubuntu", "wsl_executable": "wsl.exe"}
        atomic(b.config_file, c)
        return c["terminal"]

    def test_windows_arguments_never_parse_paths_as_commands(self):
        args = ["/usr/bin/python3", "/path with spaces/a; $(touch NEVER).py", "--runtime", "quotes ' \" ; &", "loop-peer", "id"]
        argv = command(self.terminal_config(), args)
        self.assertEqual(json.loads(base64.b64decode(argv[-1])), args)
        self.assertNotIn(";", " ".join(argv))
        self.assertEqual(argv[1:4], ["-w", "new", "new-tab"])

    def test_concurrent_open_claims_one_window(self):
        self.terminal_config(); self.begin(manual_peer=False); self.send()
        launches = []
        def launch(config, args, log):
            launches.append(args)
            self.update(peer_phase="STARTING")
            return {"launcher_exit_code": 0, "requested_at": now()}
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: open_peer(self.loops, self.id, launch), range(3)))
        self.assertEqual(len(launches), 1)
        self.assertTrue(all(r["status"] == "CONNECTED" for r in results))
        self.assertIn("--hold", launches[0])

    def test_hook_opens_automatically_and_accepts_cd_inside_project(self):
        self.terminal_config(); self.begin(manual_peer=False, session_cwd=str(self.root)); self.send()
        child = self.repo / "subfolder"
        child.mkdir()
        launched = []
        def launch(loops, identifier):
            launched.append(identifier)
            self.update(state="REVIEWING")
            self.loops.reply(self.id, fixture.result("NEEDS_FIXES"), "turn")
        with patch("terminal.open_peer", side_effect=launch):
            message = self.loops.hook(self.event(cwd=str(child)))
        self.assertIn("NEEDS_FIXES", message)
        self.assertEqual(launched, [self.id])
        with patch.object(self.loops.bridge, "current_session", return_value=("claude-1", str(child))):
            self.loops.ack(self.id, 1)
        self.assertEqual(self.loops.read(self.id)["wake_status"], "ACKNOWLEDGED")
        self.assertIsNone(self.loops.hook(self.event(cwd=str(child))))

    def test_new_plain_folder_needs_no_repo_or_commit(self):
        folder = self.root / "new standalone website"
        self.begin(project=str(folder), session_cwd=str(self.root), create_project=True, files=["index.html"])
        self.assertFalse((folder / ".git").exists())
        (folder / "index.html").write_text("<!doctype html><title>Fixture</title>")
        self.send()
        _, request = self.loops.request(self.loops.read(self.id))
        self.assertIsNone(request["head"])
        self.assertIsNone(request["branch"])
        self.assertEqual(set(request["files"]), {"index.html"})

    def test_launch_failure_is_reported_without_duplicate_retry(self):
        self.terminal_config(); self.begin(manual_peer=False); self.send()
        def broken(*args):
            raise BridgeError("Fixture launcher failure")
        with patch("terminal.open_peer", side_effect=broken):
            message = self.loops.hook(self.event())
        self.assertIn("Fixture launcher failure", message)
        self.assertEqual(self.loops.read(self.id)["state"], "FAILED")
        self.assertIsNone(self.loops.hook(self.event()))


if __name__ == "__main__":
    unittest.main()
