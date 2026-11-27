"""Shared frozen H3 protocol utilities, independent of model-editable sources."""
import hashlib
import json
import math
import os
from pathlib import Path
import xml.etree.ElementTree as ET


def load(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as inp:
        for block in iter(lambda: inp.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def percentile(values, q):
    values = sorted(values)
    if not values or any(not math.isfinite(x) or x <= 0 for x in values):
        raise ValueError("Missing or invalid measured latency")
    position = (len(values) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def environment(project, options):
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("XPUTX_", "MINIMAX_H3_")) or key in (
                "XPU_CUPTI_ENABLE_DEVICE", "XPU_TRACING_OUTPUT_NAME", "XPURT_DISPATCH_MODE",
                "XPU_ENABLE_PROFILER_TRACING", "NVTX_INJECTION64_PATH", "LD_PRELOAD"):
            env.pop(key, None)
    env.update(PYTHONPATH=os.pathsep.join((str(Path(__file__).resolve().parent), str(Path(project).resolve()))),
               INFERRSI_H3_PROJECT=str(Path(project).resolve()), PYTHONDONTWRITEBYTECODE="1",
               VLLM_WORKER_MULTIPROC_METHOD="spawn", VLLM_OMNI_VIDEO_SYNC_TIMEOUT="14400",
               PYTORCH_CUDA_ALLOC_CONF="max_split_size_mb:128")
    env.update(options)
    return env


def junit(path, returncode):
    """Missing, skipped, xfailed, collection and setup errors never imply PASS."""
    rows = list(ET.parse(path).getroot().iter("testcase"))
    if not rows or any(row.find("skipped") is not None or row.find("error") is not None for row in rows):
        raise RuntimeError("CI evidence is incomplete or skipped")
    failed = any(row.find("failure") is not None for row in rows)
    if returncode not in (0, 1) or (returncode == 0 and failed) or (returncode == 1 and not failed):
        raise RuntimeError("CI process/result mismatch")
    return {"passed": not failed, "tests": len(rows), "failures": sum(row.find("failure") is not None for row in rows)}
