import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from inferrsi_loop import controller, evaluator, runner
from inferrsi_loop.common import Busy, LoopError, copy_inputs, digest, manifest, read, write

ROOT = Path(__file__).resolve().parents[1]


def stage(protocol):
    return {"directory": str(protocol), "instructions": str(protocol / "instructions.txt"),
            "measure_command": ["{python}", "{protocol}/check.py", "--action", "measure", "--project", "{candidate}", "--result", "{result}"],
            "quality_command": ["{python}", "{protocol}/check.py", "--action", "quality", "--project", "{candidate}", "--result", "{result}"],
            "metric_key": "latency_ms", "metric_unit": "ms", "quality_key": "functional_ok", "scope": "test_only",
            "direction": "minimize", "min_improvement_pct": 0, "repetitions": 1, "warmups": 0,
            "timeout_seconds": 45, "resources": {"count": 0}}


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_job_dedup_and_artifact_integrity(self):
        source = self.root / "source"
        source.mkdir()
        job = {"argv": [sys.executable, "-c", "from pathlib import Path; p=Path(r'{output}')/'value.json'; p.write_text('{}')"],
               "cwd": str(source), "timeout_seconds": 5, "required_artifacts": ["value.json"]}
        output = self.root / "job"
        first = runner.execute(job, output)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(runner.execute(job, output), first)
        (output / "value.json").write_text('{"changed":true}')
        with self.assertRaises(LoopError):
            runner.execute(job, output)

    def test_timeout_and_missing_output_are_not_candidate_rejections(self):
        source = self.root / "source"
        source.mkdir()
        job = {"argv": [sys.executable, "-c", "import time;time.sleep(10)"],
               "cwd": str(source), "timeout_seconds": 0.1}
        self.assertEqual(runner.execute(job, self.root / "timeout")["status"], "timeout")
        job.update(argv=[sys.executable, "-c", "pass"], timeout_seconds=5, required_artifacts=["metrics.json"])
        self.assertEqual(runner.execute(job, self.root / "missing")["reason"], "missing_required_artifact")

    def test_no_idle_cards_never_launches(self):
        with patch.object(runner, "probe", return_value=[{"id": 0, "memory_used_mb": 12, "utilization_pct": 0}]):
            with self.assertRaises(Busy):
                with runner.resources({"count": 1, "lock_dir": str(self.root / "locks")}, self.root):
                    self.fail("Busy device was selected")

    def test_only_physical_zero_and_one_are_allowed(self):
        rows = [{"id": i, "memory_used_mb": 100 if i < 2 else 0, "utilization_pct": 0} for i in range(8)]
        with patch.object(runner, "probe", return_value=rows):
            with self.assertRaises(Busy):
                with runner.resources({"count": 2, "exclude": list(range(2, 8)), "lock_dir": str(self.root / "locks")}, self.root):
                    self.fail("A free but unauthorized device was selected")

    def test_lowest_idle_device_and_body_error(self):
        rows = [{"id": i, "memory_used_mb": 0, "utilization_pct": 0} for i in (0, 1, 2)]
        with patch.object(runner, "probe", return_value=rows):
            with self.assertRaises(Busy):
                with runner.resources({"count": 2, "lock_dir": str(self.root / "locks")}, self.root) as env:
                    self.assertEqual(env["XPU_VISIBLE_DEVICES"], "0,1")
                    raise Busy("A body exception must propagate, not yield a second time")

    def test_snapshot_rejects_symlink_escape(self):
        src = self.root / "src"
        src.mkdir()
        (src / "external").symlink_to("/etc/passwd")
        with self.assertRaises(LoopError):
            copy_inputs(src, self.root / "target", ["external"])

    def test_frozen_gate_tamper_blocks_resume(self):
        profile = self.root / "profile.json"
        write(profile, {"latency_ms": 1})
        cfg = {"project": {"path": str(ROOT / "examples/cpu/project"), "include": ["candidate.py"], "editable": ["candidate.py"]},
               "model": {"name": "example-model"}, "goal": "test", "enable_full": False,
               "budget": {"max_rounds": 1, "max_seconds": 100},
               "profile": {"evidence": str(profile), "bottleneck": "fixture"},
               "protocols": {"screen": stage(ROOT / "examples/cpu/protocol")}}
        run = controller.initialize(cfg, self.root / "run")
        controller.verify(run)
        config = read(run / "frozen/config.json")
        config["protocols"]["screen"]["min_improvement_pct"] = 99
        write(run / "frozen/config.json", config)
        with self.assertRaises(LoopError):
            controller.run_loop(run, resume=True)


class InferRSIIntegrationTests(unittest.TestCase):
    setUp = CoreTests.setUp
    tearDown = CoreTests.tearDown
    def test_real_campaign_quality_rejection_and_independent_final(self):
        baseline = self.root / "baseline"
        candidate = self.root / "candidate"
        copy_inputs(ROOT / "examples/cpu/project", baseline, ["candidate.py"])
        copy_inputs(baseline, candidate, ["candidate.py"])
        evidence = self.root / "profile.json"
        write(evidence, {"workload": "deterministic CPU integration fixture", "note": "not an H3 result"})
        diagnosis = {"bottleneck": "Repeated list.count scanning in the CPU fixture", "evidence": str(evidence)}
        protocol = stage(ROOT / "examples/cpu/protocol")
        (candidate / "candidate.py").write_text("def histogram(values): return {}\n")
        rejected = evaluator.assess("screen", protocol, baseline, candidate, "Deliberately wrong test candidate",
                                    self.root / "rejected", diagnosis, time.time() + 180)
        self.assertEqual(rejected["verdict"], "FAIL")
        (candidate / "candidate.py").write_text("from collections import Counter\ndef histogram(values): return dict(Counter(values))\n")
        accepted = evaluator.assess("full", protocol, baseline, candidate, "Count in one pass",
                                    self.root / "accepted", diagnosis, time.time() + 180)
        self.assertEqual(accepted["verdict"], "PASS", accepted)
        final = evaluator.assess("final", protocol, baseline, candidate, "Count in one pass",
                                self.root / "accepted", diagnosis, time.time() + 180, final=True)
        self.assertEqual(final["verdict"], "PASS", final)
        self.assertEqual(final["source_hash"], accepted["source_hash"])
        self.assertNotEqual(final["observation"], accepted["observation"])

    def test_missing_quality_field_stays_inconclusive(self):
        baseline = self.root / "baseline"
        copy_inputs(ROOT / "examples/cpu/project", baseline, ["candidate.py"])
        protocol = self.root / "protocol"
        copy_inputs(ROOT / "examples/cpu/protocol", protocol, ["check.py", "instructions.txt"])
        (protocol / "check.py").write_text((protocol / "check.py").read_text().replace('"functional_ok": all(checks)', '"missing_quality": all(checks)'))
        evidence = self.root / "profile.json"
        write(evidence, {"fixture": True})
        result = evaluator.assess("screen", stage(protocol), baseline, baseline, "Missing evidence fixture",
                                  self.root / "missing-evidence", {"bottleneck": "Fixture", "evidence": str(evidence)}, time.time() + 180)
        self.assertEqual(result["verdict"], "INCONCLUSIVE", result)


if __name__ == "__main__":
    unittest.main()
