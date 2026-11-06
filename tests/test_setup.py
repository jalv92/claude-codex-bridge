"""Portable installation tests; no real model, CLI authentication or GUI."""
import json
from pathlib import Path
import shlex
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import setup
from contracts import BridgeError
from runtime import atomic


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bridge setup ; ")
        self.root = Path(self.temp.name)
        self.project = self.root / "project $(literal)"
        self.project.mkdir()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        atomic(self.runtime / "config.json", {"schema_version": 1, "enabled": True, "projects": {}})
        self.args = SimpleNamespace(project=str(self.project), runtime=str(self.runtime),
            manual_peer=True, uninstall=False, codex=None, wt=None, distribution=None)

    def tearDown(self):
        self.temp.cleanup()

    def test_install_twice_and_remove_preserves_unrelated_hooks(self):
        settings = self.project / ".claude/settings.local.json"
        settings.parent.mkdir()
        before = {"permissions": {"allow": ["Read"]}, "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "unrelated-hook"}]}]}}
        atomic(settings, before)
        with patch("setup.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            result = setup.install(self.args)
            first = settings.read_bytes()
            setup.install(self.args)
            self.assertEqual(settings.read_bytes(), first)
            self.assertEqual(run.call_count, 2)
        command = Path(result["command"])
        self.assertTrue(command.read_text().startswith("---\n"))
        self.assertNotIn("{{", command.read_text())
        self.assertIn("Use --manual-peer", command.read_text())
        self.args.uninstall = True
        setup.install(self.args)
        setup.install(self.args)
        self.assertEqual(json.loads(settings.read_text()), before)
        self.assertFalse(command.exists())
        self.assertTrue((self.runtime / "config.json").exists())

    def test_unrelated_command_is_never_overwritten(self):
        command = self.project / ".claude/commands/codex-loop.md"
        command.parent.mkdir(parents=True)
        command.write_text("Existing user command\n")
        with patch("setup.subprocess.run") as run, self.assertRaises(BridgeError):
            setup.install(self.args)
        run.assert_not_called()
        self.assertEqual(command.read_text(), "Existing user command\n")

    def test_paths_and_metacharacters_are_quoted_in_generated_command(self):
        text = setup.render_command(self.runtime)
        command = text.split("```bash\n", 1)[1].split("\n```", 1)[0]
        self.assertEqual(shlex.split(command)[-2:], ["--runtime", str(self.runtime)])
        self.assertIn("Do not ask the user to run loop-peer", text)

    def test_symlink_command_directory_is_rejected(self):
        (self.project / ".claude").symlink_to(self.runtime, target_is_directory=True)
        with self.assertRaises(BridgeError):
            setup.manage_command(self.project, self.runtime)

    def test_configure_failure_does_not_install_hooks_or_command(self):
        with patch("setup.subprocess.run", return_value=SimpleNamespace(returncode=1, stderr="Unsupported CLI", stdout="")):
            with self.assertRaisesRegex(BridgeError, "Unsupported CLI"):
                setup.install(self.args)
        self.assertFalse((self.project / ".claude").exists())


if __name__ == "__main__":
    unittest.main()
