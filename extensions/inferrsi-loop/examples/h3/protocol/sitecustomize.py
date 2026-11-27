"""Select frozen H3 source after the image's device-free .pth bootstrap.

The image prepends its installed plugin during site initialization. Only
byte-identical, already imported startup modules may be rebound here; the
bootstrap's installed hooks remain valid because their source is unchanged.
This runs before torch, pytest, the API server, and spawned worker entrypoints.
"""
import hashlib
import importlib
import os
from pathlib import Path
import sys
import traceback


def activate(project):
    project = Path(project).resolve()
    prefix = "vllm_omni_kunlun"
    loaded = {name: module for name, module in list(sys.modules.items())
              if name == prefix or name.startswith(prefix + ".")}
    for name, module in loaded.items():
        original = Path(module.__file__).resolve()
        relative = Path(*name.split("."))
        expected = project / relative / "__init__.py" if original.name == "__init__.py" else project / relative.with_suffix(".py")
        if hashlib.sha256(original.read_bytes()).digest() != hashlib.sha256(expected.read_bytes()).digest():
            raise RuntimeError("Preloaded H3 module differs from frozen source: " + name)
    for name in loaded:
        del sys.modules[name]
    sys.path.insert(0, str(project))
    module = importlib.import_module(prefix)
    if not Path(module.__file__).resolve().is_relative_to(project):
        raise RuntimeError("Frozen H3 plugin was not selected at interpreter startup")


if os.environ.get("INFERRSI_H3_PROJECT"):
    try:
        activate(os.environ["INFERRSI_H3_PROJECT"])
    except Exception:
        # Python normally ignores sitecustomize errors. A source mismatch must
        # stop this experiment before any device work instead.
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(78)
