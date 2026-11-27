from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time


class LoopError(RuntimeError):
    pass


class Busy(LoopError):
    pass


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as inp:
        for chunk in iter(lambda: inp.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def safe_child(root, name):
    root = Path(root).resolve()
    p = root / name
    if Path(name).is_absolute() or not p.resolve().is_relative_to(root) or p.resolve() == root:
        raise LoopError("Path must stay inside its declared directory")
    if p.is_symlink():
        raise LoopError("Symbolic paths are not allowed")
    return p


def manifest(root):
    root = Path(root)
    result = {}
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            raise LoopError("Source snapshot contains a symbolic link")
        if p.is_file() and not any(x in ("__pycache__", ".git", ".pytest_cache") for x in p.relative_to(root).parts):
            result[p.relative_to(root).as_posix()] = sha(p)
    return result


def copy_inputs(source, target, includes):
    import shutil
    source, target = Path(source).resolve(), Path(target)
    if '.' in includes:
        includes = project_inputs(source)
    if target.exists():
        raise LoopError("Snapshot destination already exists")
    target.mkdir(parents=True)
    for name in includes:
        src, dst = safe_child(source, name), safe_child(target, name)
        if not src.exists():
            raise LoopError(f"Missing declared input: {name}")
        if src.is_dir():
            manifest(src)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", ".git", ".pytest_cache"))
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    return manifest(target)


@contextlib.contextmanager
def lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as out:
        try:
            fcntl.flock(out, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy("Another process holds this task/resource lock") from exc
        try:
            yield
        finally:
            fcntl.flock(out, fcntl.LOCK_UN)


def run_command(argv, cwd, output, timeout, *, env=None, stdin=None):
    """Own one process group; keep timeout/error distinct from candidate rejection."""
    if not argv or not all(isinstance(x, str) and x for x in argv) or timeout <= 0:
        raise LoopError("Explicit argv and a positive timeout are required")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with (output / "stdout.log").open("wb") as out, (output / "stderr.log").open("wb") as err:
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                             stdout=out, stderr=err, start_new_session=True)
        write(output / "process.json", {"pid": p.pid, "started_at": started, "status": "running"})
        timed_out = False
        try:
            p.communicate(None if stdin is None else stdin.encode(), timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            timed_out = True
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.communicate()
        finally:
            # A command cannot leave its children alive after returning.
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        record = {"status": "timeout" if timed_out else ("completed" if p.returncode == 0 else "error"),
                  "returncode": p.returncode, "elapsed_seconds": time.time() - started,
                  "started_at": started, "finished_at": time.time()}
        write(output / "process.json", record)
        return record


def parse_stdout(output):
    try:
        return read(Path(output) / "stdout.log")
    except (ValueError, OSError) as exc:
        raise LoopError("Command did not emit a valid JSON result; inspect its retained logs") from exc


def project_inputs(root):
    """Whole working-tree source, respecting Git ignores; snapshots need no Git."""
    root = Path(root).resolve()
    result = subprocess.run(['git', '-C', str(root), 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    if result.returncode == 0 and Path(result.stdout.strip()).resolve() == root:
        names = subprocess.check_output(['git', '-C', str(root), 'ls-files', '--cached', '--others', '--exclude-standard', '-z']).decode().split('\0')
        return sorted({name for name in names if name and safe_child(root, name).is_file()})
    return list(manifest(root))
