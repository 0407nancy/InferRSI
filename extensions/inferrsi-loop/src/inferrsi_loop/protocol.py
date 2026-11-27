"""Frozen protocol scripts call this bridge to retain Runner jobs and artifacts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .common import LoopError, read, safe_child, write
from .runner import execute


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--spec", type=Path, required=True)
    p.add_argument("--action", choices=("measure", "quality"), required=True)
    p.add_argument("--project", type=Path, required=True)
    p.add_argument("--result", type=Path, required=True)
    a = p.parse_args()
    spec = read(a.spec)[a.action]
    output = a.result.resolve().parent / "runner-job"
    replacements = {"{project}": str(a.project.resolve()), "{protocol}": str(a.spec.resolve().parent),
                    "{python}": sys.executable, "{output}": str(output)}
    argv = list(spec["argv"])
    for old, new in replacements.items():
        argv = [x.replace(old, new) for x in argv]
    job = {"argv": argv, "cwd": str(a.project.resolve()), "timeout_seconds": spec["timeout_seconds"],
           "required_artifacts": [spec["result_file"]], "input_hashes": spec.get("input_hashes", {})}
    result = execute(job, output)
    if result["status"] != "completed":
        # Missing/error evidence must not be converted into quality=false (candidate rejection).
        write(a.result, {"execution_status": result["status"], "reason": result.get("reason")})
        return 2
    document = read(safe_child(output, spec["result_file"]))
    if not isinstance(document, dict):
        raise LoopError("Protocol result must be an object")
    document["runner_evidence"] = str(output / "result.json")
    write(a.result, document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
