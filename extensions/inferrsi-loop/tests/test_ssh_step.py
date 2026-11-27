import base64
from pathlib import Path
import tempfile
import unittest

from inferrsi_loop import controller, ssh_step
from inferrsi_loop.common import LoopError, digest, manifest, read, write

ROOT = Path(__file__).resolve().parents[1]


class LocalSSHTests(unittest.TestCase):
    def setUp(self):
        from test_loop import stage
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        evidence = self.root / "profile.json"
        write(evidence, {"scope": "CPU transport fixture"})
        self.config = {"model": {"name": "gpt-6-astra", "transport": "local_ssh"}, "goal": "test",
                       "project": {"path": str(ROOT / "examples/cpu/project"), "include": ["candidate.py"], "editable": ["candidate.py"]},
                       "enable_full": False, "profile": {"evidence": str(evidence), "bottleneck": "fixture"},
                       "protocols": {"screen": stage(ROOT / "examples/cpu/protocol")},
                       "budget": {"max_rounds": 1, "max_seconds": 300}}
        self.run = controller.initialize(self.config, self.root / "remote-run")
        result = controller.run_loop(self.run)
        self.assertEqual(result["status"], "awaiting_local_proposal")
        self.plan = ssh_step.dispatch({"action": "plan", "run": str(self.run)})

    def tearDown(self):
        self.temp.cleanup()

    def submission(self):
        local = self.root / "local-candidate"
        local.mkdir()
        (local / "candidate.py").write_text("from collections import Counter\ndef histogram(values):return dict(Counter(values))\n")
        return {"action": "submit", "run": str(self.run), "files": {"candidate.py": base64.b64encode((local / "candidate.py").read_bytes()).decode()},
                "proposal": {"context_digest": digest(self.plan["context"]), "source_hash": digest(manifest(local)), "changed_files": ["candidate.py"],
                             "hypothesis": "test handoff", "model": "gpt-6-astra"}, "patch": "fixture", "events": "", "usage": {}}

    def test_handoff_is_idempotent_and_model_stays_local(self):
        submission = self.submission()
        self.assertEqual(ssh_step.dispatch(submission)["status"], "recorded")
        self.assertEqual(ssh_step.dispatch(submission)["status"], "recorded")
        proposal = read(self.run / "rounds/1/proposal.json")
        self.assertEqual(proposal["model_execution_host"], "local_mac")

    def test_different_context_cannot_be_submitted(self):
        submission = self.submission()
        submission["proposal"]["context_digest"] = "changed"
        with self.assertRaises(LoopError):
            ssh_step.dispatch(submission)
        self.assertFalse((self.run / "rounds/1/candidate").exists())

    def test_quality_gate_edit_cannot_be_submitted(self):
        submission = self.submission()
        submission["files"]["acceptance.py"] = base64.b64encode(b"pass").decode()
        with self.assertRaises(LoopError):
            ssh_step.dispatch(submission)
        self.assertFalse((self.run / "rounds/1/candidate").exists())
