"""Behavioral tests for the local coding tools; no network or model calls."""

import hashlib
import json
from pathlib import Path
import sys
import unittest
import uuid

from agent_core import Config, EventLogger
from coding_agent import CodingTools


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / ".local" / "tests"


def new_workspace(name):
    path = TEST_ROOT / f"{name}-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    return path


class CodingToolsTests(unittest.TestCase):
    def setUp(self):
        self.workspace = new_workspace(self._testMethodName)
        self.run_dir = self.workspace / "run"
        self.logger = EventLogger(self.run_dir, stage="test")
        self.tools = CodingTools(self.workspace, self.logger, Config(command_timeout_s=2))

    def test_paths_stay_in_workspace_and_protect_metadata(self):
        outside = self.workspace.parent / f"outside-{uuid.uuid4().hex}.txt"
        for path in ("../outside.txt", str(outside), ".git/config", ".env"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.tools.write_file(path, "unsafe")
        self.assertFalse(outside.exists())
        self.assertFalse((self.workspace / ".git").exists())

    def test_allowed_and_readonly_writes(self):
        tools = CodingTools(
            self.workspace, self.logger, Config(),
            allowed=["allowed.py", "locked.py"], readonly=["locked.py"],
        )
        self.assertTrue(tools.write_file("allowed.py", "pass\n")["ok"])
        with self.assertRaisesRegex(ValueError, "authorized write"):
            tools.write_file("other.py", "pass\n")
        with self.assertRaisesRegex(ValueError, "immutable"):
            tools.write_file("locked.py", "pass\n")
        self.assertFalse((self.workspace / "other.py").exists())
        self.assertFalse((self.workspace / "locked.py").exists())

    def test_unique_replace_and_change_evidence(self):
        first = self.tools.write_file("program.py", "answer = 1\nanswer = 1\n")
        with self.assertRaisesRegex(ValueError, "exactly once"):
            self.tools.replace_text("program.py", "answer = 1", "answer = 2")
        self.assertEqual((self.workspace / "program.py").read_text(), "answer = 1\nanswer = 1\n")
        second = self.tools.replace_text("program.py", "answer = 1\nanswer = 1", "answer = 2")
        raw = (self.workspace / "program.py").read_bytes()
        self.assertEqual(second["before_sha256"], first["after_sha256"])
        self.assertEqual(second["after_sha256"], hashlib.sha256(raw).hexdigest())
        patch = Path(second["diff_path"]).read_text(encoding="utf-8")
        self.assertIn("-answer = 1", patch)
        self.assertIn("+answer = 2", patch)
        events = [json.loads(x) for x in self.logger.events_path.read_text().splitlines()]
        self.assertEqual(len([x for x in events if x["type"] == "file_changed"]), 2)

    def test_subprocess_reports_real_output_failures_and_timeout(self):
        ok = self.tools.run_process([sys.executable, "-c", "print('hello')"])
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["stdout"].strip(), "hello")
        self.assertEqual(Path(ok["stdout_path"]).read_text().strip(), "hello")
        bad = self.tools.run_process([sys.executable, "-c", "import sys; print('bad', file=sys.stderr); sys.exit(7)"])
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["exit_code"], 7)
        self.assertIn("bad", bad["stderr"])
        slow = self.tools.run_process([sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
        self.assertFalse(slow["ok"])
        self.assertTrue(slow["timed_out"])

    def test_destructive_commands_are_rejected_before_execution(self):
        for argv in (["rm", "target"], ["powershell", "-Command", "Remove-Item target"],
                     ["git", "clean", "-fd"], [sys.executable, "-c", "import os; os.remove('target')"]):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                self.tools.run_process(argv)
        self.assertEqual(self.tools.process_count, 0)

    def test_finish_uses_unchanged_acceptance_and_recovers_after_fix(self):
        check = self.workspace / "acceptance.py"
        check.write_text(
            "from pathlib import Path\n"
            "import sys\n"
            "actual = Path('program.txt').read_text(encoding='utf-8')\n"
            "sys.exit(0 if actual == 'correct\\n' else 9)\n",
            encoding="utf-8",
        )
        original_hash = hashlib.sha256(check.read_bytes()).hexdigest()
        tools = CodingTools(
            self.workspace, self.logger, Config(command_timeout_s=2),
            acceptance=[[sys.executable, str(check)]],
            allowed=["program.txt"], readonly=["acceptance.py"],
        )
        tools.write_file("program.txt", "wrong\n")
        failed = tools.finish("looks done")
        self.assertFalse(failed["accepted"])
        self.assertEqual(failed["checks"][0]["exit_code"], 9)
        tools.write_file("program.txt", "correct\n")
        passed = tools.finish("fixed")
        self.assertTrue(passed["accepted"])
        self.assertEqual(passed["checks"][0]["exit_code"], 0)
        self.assertEqual(hashlib.sha256(check.read_bytes()).hexdigest(), original_hash)
        self.assertEqual(passed["changed_files"], ["program.txt"])


if __name__ == "__main__":
    unittest.main()
