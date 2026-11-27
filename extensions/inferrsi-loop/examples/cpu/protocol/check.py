"""Small actual CPU workload; it provides no evidence about H3 performance."""
import argparse
from collections import Counter
import importlib.util
import json
from pathlib import Path
import random
import statistics
import time

p = argparse.ArgumentParser()
p.add_argument("--project", required=True)
p.add_argument("--result", required=True)
p.add_argument("--action", choices=("quality", "measure", "profile"), required=True)
a = p.parse_args()
spec = importlib.util.spec_from_file_location("candidate", Path(a.project) / "candidate.py")
candidate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(candidate)
rng = random.Random(7821)
inputs = [[], [1, 1, -2, 7], [rng.randrange(-512, 512) for _ in range(30000)]]
checks = []
for values in inputs:
    before = list(values)
    actual = candidate.histogram(values)
    checks.append(actual == dict(Counter(values)) and values == before)
result = {"functional_ok": all(checks), "scope": "cpu_loop_integration_only", "checks": checks}
if a.action != "quality":
    samples = []
    for i in range(5):
        t = time.perf_counter()
        candidate.histogram(inputs[-1])
        elapsed = (time.perf_counter() - t) * 1000
        if i >= 2:
            samples.append(elapsed)
    result.update(latency_ms=statistics.median(samples), samples_ms=samples, warmups=2, measurements=3)
out = Path(a.result)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(result, indent=2))
