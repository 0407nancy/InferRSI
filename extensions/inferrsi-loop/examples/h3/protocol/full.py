"""Official H3 requests and unchanged CI; timing never enables a profiler."""
import argparse
from pathlib import Path
import subprocess
import sys

from support import environment, junit, load, save, sha


def run_ci(argv, output, env, timeout):
    output.mkdir(parents=True, exist_ok=True)
    report = output / "junit.xml"
    with (output / "stdout.log").open("w") as out, (output / "stderr.log").open("w") as err:
        proc = subprocess.run([*argv, "--junitxml=" + str(report)], env=env, cwd=output,
                              stdout=out, stderr=err, timeout=timeout)
    return junit(report, proc.returncode)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--action", choices=("quality", "measure"), required=True)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    spec = load(args.spec)
    project, output = args.project.resolve(), args.result.resolve().parent
    output.mkdir(parents=True, exist_ok=True)
    env = environment(project, spec["environment"])
    env["INFERRSI_H3_PROJECT"] = str(project)
    for path, expected in spec["recipe_hashes"].items():
        if sha(path) != expected:
            raise RuntimeError("The frozen H3 recipe changed")
    short = spec.get("validation_scope") == "short_functional"
    result = {"scope": "h3_short_starship_dinner" if short else "h3_official_starship_dinner", "cases": {}, "ci": {},
              "warmups": spec.get("warmups", 2) if args.action == "measure" else 0,
              "measurements": spec.get("measurements", 3) if args.action == "measure" else 1,
              "human_review": "pending"}
    if args.action == "quality" and not short:
        numerical = output / "numerical"
        numerical.mkdir()
        command = [sys.executable, "-m", "torch.distributed.run", "--standalone", "--nproc_per_node=2",
                   str(root / "component.py"), "--project", str(project), "--result", str(numerical / "summary.json"),
                   "--spec", str(args.spec), "--action", "numerical"]
        with (numerical / "execution.log").open("w") as log:
            subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=3600)
        numerical_result = load(numerical / "summary.json")
        result["ci"]["real_input_numerical"] = numerical_result
        precision = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                     *[str(root / "ci" / name) for name in spec["precision_tests"]]]
        result["ci"]["existing_precision_ci"] = run_ci(precision, output / "precision-ci", env, 7200)
        if not numerical_result["functional_ok"] or not result["ci"]["existing_precision_ci"]["passed"]:
            result["quality_ok"] = False
            save(args.result, result)
            return
    for case, config in spec["cases"].items():
        folder = output / case
        request = root / config["request"]
        image = root / config["first_frame"] if config.get("first_frame") else None
        if image and sha(image) != config["first_frame_sha256"]:
            raise RuntimeError("Official first frame changed")
        command = [sys.executable, str(root / "recipe.py"), "--recipe", spec["performance_recipe"],
                   "--project", str(project), "--", "--model", "/models/MiniMax-H3", "--request", str(request),
                   "--xpu-ids", env["XPU_VISIBLE_DEVICES"], "--result-dir", str(folder),
                   "--warmups", str(result["warmups"]), "--measures", str(result["measurements"])]
        if image:
            command += ["--first-frame", str(image)]
        with (output / (case + "-recipe.log")).open("w") as log:
            subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=43200)
        measured = load(folder / "metrics.json")
        if measured["success_count"] != result["measurements"] or measured["warmups"] != result["warmups"]:
            raise RuntimeError("The recipe did not complete all requested samples")
        result["cases"][case] = {"p95_s": measured["p95_s"], "mean_s": measured["mean_s"],
                                 "throughput_rps": 1 / measured["mean_s"]}
        video = folder / f'measure_{result["measurements"]:02d}.mp4'
        if args.action == "quality" and short:
            result["ci"][case] = {"passed": True, "scope": "recipe_functional_video_validation"}
        if args.action == "quality" and not short:
            inputs, scores = folder / "quality-inputs", folder / "quality-scores"
            inputs.mkdir()
            import shutil
            shutil.copy2(video, inputs / (case + ".mp4"))
            if image:
                shutil.copy2(image, inputs / "first.png")
            quality_env = dict(env, XPU_VISIBLE_DEVICES="", CUDA_VISIBLE_DEVICES="", OMP_NUM_THREADS="8", MKL_NUM_THREADS="8",
                               HF_HOME=spec["quality_cache"] + "/huggingface", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                               H3_EVAL_CACHE=spec["quality_cache"], H3_QUALITY_INPUTS=str(inputs), H3_QUALITY_RESULTS=str(scores))
            test = root / "ci/tests/models/minimax_h3/accuracy/test_quality.py"
            gate = run_ci([spec["quality_python"], "-m", "pytest", "-q", "-p", "no:cacheprovider", str(test), "-k", case],
                          folder / "quality-ci", quality_env, 14400)
            evidence = load(scores / (case + ".json"))
            expected = load(root / "ci/tests/models/minimax_h3/accuracy/cases.json")["cases"][case]["scores"]
            if set(evidence["scores"]) != set(expected) or any(type(row.get("passed")) is not bool for row in evidence["scores"].values()):
                raise RuntimeError("Incomplete quality score evidence")
            if evidence["video_sha256"] != sha(video):
                raise RuntimeError("Quality CI scored a different video")
            gate["passed"] = gate["passed"] and all(row["passed"] for row in evidence["scores"].values())
            result["ci"][case] = gate
    if args.action == "quality":
        result["quality_ok"] = all(result["ci"][case]["passed"] for case in spec["cases"])
    else:
        result["latency_s"] = sum(row["mean_s"] for row in result["cases"].values())
    save(args.result, result)


if __name__ == "__main__":
    main()
