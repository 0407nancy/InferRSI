"""Use candidate source in the API process and all spawned workers."""
import os
from pathlib import Path
import runpy
import sys

project = Path(os.environ["INFERRSI_H3_PROJECT"]).resolve()
sys.path.insert(0, str(project))
import vllm_omni_kunlun

if not Path(vllm_omni_kunlun.__file__).resolve().is_relative_to(project):
    raise RuntimeError("Spawned H3 process imported a different plugin")
print("[inferrsi-h3] candidate-plugin=" + str(vllm_omni_kunlun.__file__), flush=True)
if __name__ == "__main__":
    runpy.run_module("vllm_omni.entrypoints.cli.main", run_name="__main__")
