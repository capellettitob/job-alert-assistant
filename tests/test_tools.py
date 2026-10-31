"""Runs the demo and the Apps Script tests as part of the one test command."""
import os
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Demo(unittest.TestCase):
    def test_demo_runs_with_an_empty_environment_and_prints_a_ping(self):
        # An empty environment: no keys, no tokens, nothing to talk to.
        out = subprocess.run([sys.executable, os.path.join(ROOT, "demo.py")], cwd=ROOT, env={"PATH": os.environ.get("PATH", "")},
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("New job: Clean up and merge three sales spreadsheets", out.stdout)
        self.assertIn("[ Go ]   [ Skip ]", out.stdout)
        self.assertIn("No ping.", out.stdout)

    def test_demo_run_2_uses_the_labelled_fake_model_and_shows_the_ai_check(self):
        out = subprocess.run([sys.executable, os.path.join(ROOT, "demo.py")], cwd=ROOT, env={"PATH": os.environ.get("PATH", "")},
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        run_1, run_2 = out.stdout.split("RUN 2:")
        self.assertNotIn("AI check", run_1, "with the AI off the ping has no AI section")
        self.assertIn("FAKE model", run_2)
        self.assertIn("No network call", run_2)
        self.assertIn("AI check: confidence 97 out of 100", run_2)
        self.assertIn("not a measured accuracy", run_2)
        self.assertIn("Model: FAKE MODEL", run_2)
        self.assertIn("written by the AI step", run_2)
        self.assertIn("AI helped me draft this reply. I read and check every detail myself.", run_2)
        self.assertNotIn("AI helped me", run_1, "the template draft never carries the disclosure line")
        self.assertIn("No ping. A silent note is posted to Telegram instead", run_2)
        self.assertIn("Skipped: Python dashboard plus a native mobile app with payments. AI check: the model says it cannot deliver this", run_2)
        self.assertEqual(out.stderr, "")

    def test_the_score_command_works_offline(self):
        job = os.path.join(ROOT, "tests", "sample_job.json")
        out = subprocess.run([sys.executable, "-m", "bot", "score", job, "--config", os.path.join(ROOT, "config.example.json")],
                             cwd=ROOT, env={"PATH": os.environ.get("PATH", "")}, capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("New job:", out.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed, so the Apps Script tests are skipped")
class Watcher(unittest.TestCase):
    def test_apps_script_tests_pass(self):
        out = subprocess.run(["node", os.path.join(ROOT, "tests", "watcher.test.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
