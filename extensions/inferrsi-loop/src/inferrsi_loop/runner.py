from __future__ import annotations

import contextlib
import os
from pathlib import Path
import shlex
import subprocess
import threading
import time

from .common import Busy, LoopError, digest, lock, manifest, read, run_command, write


def probe():
    """Read xpu-smi's documented machine output; never initialize a device."""
    p = subprocess.run(["xpu-smi", "-m"], capture_output=True, text=True, timeout=15, check=True)
    devices = []
    for line in p.stdout.splitlines():
        values = shlex.split(line)
        if len(values) < 23:
            continue
        try:
            devices.append({"id": int(values[2]), "memory_used_mb": int(values[17]),
                            "utilization_pct": int(values[19]), "model": values[21]})
        except (ValueError, IndexError):
            continue
    if not devices or len({d["id"] for d in devices}) != len(devices):
        raise LoopError("Device probe returned no unambiguous devices")
    return sorted(devices, key=lambda d: d["id"])


@contextlib.contextmanager
def resources(settings, output):
    """An entire paired evaluation holds the same lease, including warmups."""
    count = int(settings.get("count", 0))
    if count < 0:
        raise LoopError("Negative device count")
    env = dict(os.environ)
    if not count:
        env.update(XPU_VISIBLE_DEVICES="", CUDA_VISIBLE_DEVICES="")
        yield env
        return
    lock_root = Path(settings["lock_dir"])
    excluded = settings.get("exclude", [])
    until = time.monotonic() + settings.get("wait_seconds", 0)
    while True:
        stack = contextlib.ExitStack()
        try:
            for shared in settings.get("shared_locks", []):
                stack.enter_context(lock(shared))
            rows = probe()
            if settings.get("require_host_idle") and any(d["memory_used_mb"] or d["utilization_pct"] for d in rows):
                raise Busy("Formal evaluation requires an idle host device set")
            idle = [d for d in rows if d["memory_used_mb"] == 0 and d["utilization_pct"] == 0 and d["id"] not in excluded
                    and ('allow' not in settings or d['id'] in settings['allow'])]
            if len(idle) < count:
                raise Busy("No idle device group; existing jobs were left untouched")
            selected = []
            for device in idle:
                try:
                    stack.enter_context(lock(lock_root / f'device-{device["id"]}.lock'))
                    selected.append(device["id"])
                except Busy:
                    continue
                if len(selected) == count:
                    break
            if len(selected) != count:
                raise Busy("Idle devices are reserved by another task")
            after = {d["id"]: d for d in probe()}
            if any(after[i]["memory_used_mb"] or after[i]["utilization_pct"] for i in selected):
                raise Busy("Devices became busy before launch")
            env.update(XPU_VISIBLE_DEVICES=",".join(map(str, selected)), CUDA_VISIBLE_DEVICES=",".join(map(str, selected)))
            write(Path(output) / "devices.json", {"selected": selected, "probe": rows, "reserved_at": time.time()})
        except Busy:
            stack.close()
            if time.monotonic() >= until:
                raise
            time.sleep(min(2, max(0, until - time.monotonic())))
            continue
        except BaseException:
            stack.close()
            raise
        stop = threading.Event()
        occupancy = {"external_activity": False, "probe_failed": False, "checks": 0}
        def audit():
            while not stop.wait(2):
                try:
                    current = probe()
                    occupancy["checks"] += 1
                    if any((d["memory_used_mb"] or d["utilization_pct"]) and d["id"] not in selected for d in current):
                        occupancy["external_activity"] = True
                except (OSError, ValueError, subprocess.SubprocessError, LoopError):
                    occupancy["probe_failed"] = True
        monitor = None
        if settings.get("require_host_idle"):
            monitor = threading.Thread(target=audit, daemon=True)
            monitor.start()
        try:
            yield env
        finally:
            stop.set()
            if monitor:
                monitor.join(timeout=20)
                if monitor.is_alive():
                    occupancy["probe_failed"] = True
                write(Path(output) / "occupancy.json", occupancy)
            stack.close()
        return


def execute(job, directory, *, env=None):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    signature = digest(job)
    with lock(directory / ".lock"):
        receipt = directory / "result.json"
        if receipt.exists():
            previous = read(receipt)
            if previous.get("job_digest") != signature:
                raise LoopError("Job ID was reused with different inputs")
            from .common import safe_child, sha
            for name, expected in previous.get("artifacts", {}).items():
                if sha(safe_child(directory, name)) != expected:
                    raise LoopError("A retained job artifact changed")
            if previous.get("source_hash") is not None and digest(manifest(job["cwd"])) != previous["source_hash"]:
                raise LoopError("Source changed since this job completed")
            return previous
        if (directory / "job.json").exists():
            raise LoopError("Unsettled job exists; inspect it before explicit recovery")
        write(directory / "job.json", job)
        source = Path(job["cwd"]).resolve()
        before = manifest(source) if job.get("verify_source", True) else None
        for name, expected in job.get("input_hashes", {}).items():
            from .common import sha
            if sha(name) != expected:
                raise LoopError("A frozen job input changed")
        argv = [x.replace("{output}", str(directory)).replace("{source}", str(source)) for x in job["argv"]]
        env = dict(os.environ if env is None else env, PYTHONDONTWRITEBYTECODE="1")
        result = run_command(argv, source, directory / "execution", job["timeout_seconds"], env=env)
        if before is not None and manifest(source) != before:
            result.update(status="error", reason="source_changed_during_execution")
        artifacts = {}
        if result["status"] == "completed":
            from .common import safe_child, sha
            for name in job.get("required_artifacts", []):
                path = safe_child(directory, name)
                if not path.is_file():
                    result.update(status="error", reason="missing_required_artifact")
                else:
                    artifacts[name] = sha(path)
        result.update(job_digest=signature, artifacts=artifacts, source_hash=digest(before) if before is not None else None)
        write(receipt, result)
        return result
