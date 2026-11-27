from __future__ import annotations

import json
import os
import platform
from pathlib import Path
import shutil
import subprocess

from .common import LoopError, run_command, write


def model_environment(settings):
    env = dict(os.environ)
    if settings.get("codex_home"):
        env["CODEX_HOME"] = str(Path(settings["codex_home"]).resolve())
    # The agent generates code, not accelerator workloads; only Runner reserves cards.
    env.update(XPU_VISIBLE_DEVICES="", CUDA_VISIBLE_DEVICES="")
    return env


def command(settings, workspace, schema, answer):
    if platform.system() != "Darwin":
        raise LoopError("Model calls must run through Codex on the user's local Mac")
    binary = shutil.which(settings.get("codex", "codex"))
    if not binary:
        raise LoopError("The local Codex CLI is not installed")
    if not settings.get("name"):
        raise LoopError("An explicit model name is required")
    argv = [binary, "exec", "--model", settings["name"], "--sandbox", "workspace-write",
            "--ephemeral", "--skip-git-repo-check", "--json", "--color", "never",
            "--output-schema", str(schema), "--output-last-message", str(answer),
            "--cd", str(workspace)]
    if settings.get("effort"):
        argv += ["-c", "model_reasoning_effort=" + json.dumps(settings["effort"])]
    return argv + ["-"]


def doctor(settings, output, *, probe_model=False):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if platform.system() != "Darwin":
        raise LoopError("Model access is local-only; use local_driver.py over SSH")
    binary = shutil.which(settings.get("codex", "codex"))
    checks = {"codex_installed": bool(binary), "model_configured": bool(settings.get("name")),
              "model_call": "not_run"}
    runtime = {"model": settings.get("name"), "reasoning_effort": settings.get("effort"),
               "codex": binary, "model_execution_host": "local_mac"}
    if binary:
        p = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20)
        checks["codex_executable"] = p.returncode == 0
        runtime["codex_version"] = p.stdout.strip()
    if probe_model and binary and settings.get("name"):
        workspace = output / "probe-workspace"
        workspace.mkdir(exist_ok=True)
        schema, answer = output / "probe-schema.json", output / "probe-answer.json"
        write(schema, {"type": "object", "properties": {"ready": {"type": "boolean"}},
                       "required": ["ready"], "additionalProperties": False})
        result = run_command(command(settings, workspace, schema, answer), workspace,
                             output / "probe-execution", settings.get("timeout_seconds", 120),
                             env=model_environment(settings),
                             stdin='Do not execute commands or read other files. Return {"ready":true}.')
        try:
            checks["model_call"] = result["status"] == "completed" and json.loads(answer.read_text()) == {"ready": True}
        except (OSError, ValueError):
            checks["model_call"] = False
    checks["ready"] = bool(binary and checks.get("codex_executable") and checks["model_configured"]
                           and (not probe_model or checks["model_call"] is True))
    write(output / "runtime.json", runtime)
    write(output / "checks.json", checks)
    return checks
