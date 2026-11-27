import argparse
import json
from pathlib import Path
import sys

p = argparse.ArgumentParser()
p.add_argument("--output", type=Path, required=True)
p.add_argument("--model", required=True)
p.add_argument("--effort", default="medium")
a = p.parse_args()
root = Path(__file__).resolve().parents[1] / "examples/cpu"
protocol = root / "protocol"
stage = {"directory": str(protocol), "instructions": str(protocol / "instructions.txt"),
         "measure_command": ["{python}", "{protocol}/check.py", "--action", "measure", "--project", "{candidate}", "--result", "{result}"],
         "quality_command": ["{python}", "{protocol}/check.py", "--action", "quality", "--project", "{candidate}", "--result", "{result}"],
         "metric_key": "latency_ms", "metric_unit": "ms", "quality_key": "functional_ok", "scope": "cpu_loop_integration_only",
         "direction": "minimize", "min_improvement_pct": 0, "repetitions": 1, "warmups": 0,
         "timeout_seconds": 90, "resources": {"count": 0}}
cfg = {"schema_version": 1, "goal": "Improve the histogram CPU implementation. Use measured feedback for a distinct second candidate. This is only a loop integration test.",
       "model": {"name": a.model, "effort": a.effort, "transport": "local_ssh", "timeout_seconds": 900},
       "project": {"path": str(root / "project"), "include": ["candidate.py"], "editable": ["candidate.py"]},
       "profile": {"argv": [sys.executable, str(protocol / "check.py"), "--project", "{source}", "--result", "{output}/summary.json", "--action", "profile"],
                   "summary_file": "summary.json", "timeout_seconds": 60, "resources": {"count": 0}, "bottleneck": "Measured repeated counting in the frozen CPU fixture"},
       "protocols": {"screen": stage, "full": dict(stage)}, "budget": {"max_rounds": 2, "max_seconds": 1800, "max_infra_retries": 1},
       "min_rounds_before_full": 2, "enable_full": True, "human_review_required": False}
a.output.parent.mkdir(parents=True, exist_ok=True)
a.output.write_text(json.dumps(cfg, indent=2))
