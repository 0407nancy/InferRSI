import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class H3StartupTests(unittest.TestCase):
    def run_startup(self, changed):
        startup = Path(__file__).resolve().parents[1] / "examples/h3/protocol/sitecustomize.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for source in ("installed", "candidate"):
                package = root / source / "vllm_omni_kunlun"
                package.mkdir(parents=True)
                (package / "__init__.py").write_text("")
                (package / "bootstrap.py").write_text("value = 2\n" if changed and source == "candidate" else "value = 1\n")
                (package / "projection.py").write_text(f"source = {source!r}\n")
            code = """import os, runpy, sys
sys.path.insert(0, sys.argv[1])
import vllm_omni_kunlun.bootstrap
os.environ['INFERRSI_H3_PROJECT'] = sys.argv[2]
runpy.run_path(sys.argv[3])
from vllm_omni_kunlun import projection
assert projection.source == 'candidate'
from pathlib import Path
assert Path(projection.__file__).resolve().is_relative_to(Path(sys.argv[2]).resolve())
"""
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            env.pop("INFERRSI_H3_PROJECT", None)
            return subprocess.run([sys.executable, "-c", code, str(root / "installed"), str(root / "candidate"), str(startup)],
                                  env=env, capture_output=True, text=True, timeout=20)

    def test_identical_bootstrap_then_candidate_projection(self):
        result = self.run_startup(False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_changed_preloaded_module_fails_before_execution(self):
        result = self.run_startup(True)
        self.assertEqual(result.returncode, 78)
        self.assertIn("Preloaded H3 module differs", result.stderr)
