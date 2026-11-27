"""Reuse the existing H3 recipe; only inject the retained candidate source."""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import sys

p = argparse.ArgumentParser()
p.add_argument("--recipe", type=Path, required=True)
p.add_argument("--project", type=Path, required=True)
p.add_argument("arguments", nargs=argparse.REMAINDER)
a = p.parse_args()
original = subprocess.Popen


def launch(command, *args, **kwargs):
    if isinstance(command, (list, tuple)) and "vllm_omni.entrypoints.cli.main" in command:
        index = command.index("vllm_omni.entrypoints.cli.main")
        command = [command[0], str(Path(__file__).with_name("server.py")), *command[index + 1:]]
        kwargs["env"] = dict(kwargs.get("env") or os.environ,
                             PYTHONPATH=os.pathsep.join((str(Path(__file__).resolve().parent), str(a.project.resolve()))),
                             INFERRSI_H3_PROJECT=str(a.project.resolve()))
    return original(command, *args, **kwargs)


subprocess.Popen = launch
sys.path.insert(0, str(a.recipe.parent))
sys.argv = [str(a.recipe), *[x for x in a.arguments if x != "--"]]
runpy.run_path(str(a.recipe), run_name="__main__")
