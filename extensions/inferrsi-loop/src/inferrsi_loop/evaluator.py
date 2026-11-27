from __future__ import annotations

import os
from pathlib import Path
import shlex
import sys
import time

from .common import LoopError, digest, manifest, parse_stdout, read, run_command, sha, write
from .runner import resources


def cli(arguments, directory, *, timeout, env=None):
    result = run_command([sys.executable, "-m", "inferrsi", *arguments], Path.cwd(), directory,
                         timeout, env=env)
    if result["status"] == "completed":
        return parse_stdout(directory)
    # An INCONCLUSIVE assessment deliberately exits nonzero. Only accept it
    # when the CLI returned a retained observation, never an error-shaped JSON.
    if result["status"] == "error" and arguments[:2] in (["campaign", "round"], ["campaign", "final"]):
        value = parse_stdout(directory)
        observation = value.get("latest_observation") or value.get("final")
        if isinstance(observation, dict) and observation.get("assessment", {}).get("verdict") == "INCONCLUSIVE":
            return value
    raise LoopError("InferRSI command failed; inspect its retained logs")


def start(stage, baseline, session, output, diagnosis, deadline):
    remaining = deadline - time.time()
    if remaining < 10:
        raise LoopError("No evaluation budget remains")
    args = ["campaign", "start", "--repo", str(baseline), "--protocol", str(stage["directory"]),
            "--instructions", str(stage["instructions"]), "--session", str(session),
            "--rounds", "1", "--round-mode", "up_to", "--max-minutes", str(remaining / 60),
            "--evaluation-timeout-seconds", str(min(stage["timeout_seconds"], remaining)),
            "--final-reserve-seconds", str(min(stage.get("final_reserve_seconds", 1), remaining / 4)),
            "--condition", "codex-agent-loop", "--knowledge", "off"]
    # This is already the isolated, declared project snapshot. Retain the same
    # directory boundary for each candidate so newly added sources are included.
    args += ["--include", "."]
    for key in ("metric_key", "metric_unit", "quality_key", "scope", "direction", "min_improvement_pct",
                "repetitions", "warmups"):
        args += ["--" + key.replace("_", "-"), str(stage[key])]
    for key in ("measure_command", "quality_command"):
        args += ["--" + key.replace("_", "-"), shlex.join(stage[key])]
    cli(args, output / "start", timeout=min(120, remaining))
    cli(["campaign", "diagnose", "--session", str(session), "--bottleneck", diagnosis["bottleneck"],
         "--evidence", diagnosis["evidence"]], output / "diagnose", timeout=60)


def native_change(baseline, candidate):
    before, after = manifest(baseline), manifest(candidate)
    changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    return any(Path(name).suffix in ('.c', '.cc', '.cpp', '.cxx', '.cu', '.cuh', '.h', '.hpp', '.rs', '.cmake')
               or Path(name).name in ('setup.py', 'setup.cfg', 'pyproject.toml', 'CMakeLists.txt') for name in changed)


def verify_native_evidence(session, observed, expected):
    steps = observed.get('execution', {}).get('steps', [])
    if not steps:
        raise LoopError('Missing native build/load evidence')
    seen = set()
    for step in steps:
        side = step['id'].rsplit('-', 1)[-1]
        if side not in expected:
            continue
        seen.add(side)
        value = read(session / observed['path'] / 'execution' / step['id'] / 'result.json').get('native_build', {})
        if value.get('source_hash') != expected[side] or not value.get('loaded_artifacts'):
            raise LoopError('Native build/load source fingerprint missing or stale')
        for artifact in value['loaded_artifacts']:
            if artifact.get('loaded') is not True or sha(artifact['path']) != artifact['sha256']:
                raise LoopError('Native candidate loaded-artifact proof is invalid')
    if seen != set(expected):
        raise LoopError('Incomplete native build/load evidence for paired sources')


def assess(stage_name, stage, baseline, candidate, hypothesis, output, diagnosis, deadline, *, final=False):
    """One frozen campaign per candidate/stage. Final reuses that full campaign."""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    session = output / "campaign"
    receipt = output / ("final-decision.json" if final else "decision.json")
    source_hash = digest(manifest(candidate))
    native = native_change(baseline, candidate)
    if native and not stage.get("native_build_contract"):
        raise LoopError("Native/build inputs changed: supply a frozen rebuild-and-load verification protocol before measuring")
    expected_native = {"baseline": digest(manifest(baseline)), "candidate": source_hash} if native else None
    if receipt.exists():
        previous = read(receipt)
        if previous["source_hash"] != source_hash:
            raise LoopError("Candidate differs from recorded evaluation")
        return previous
    if not session.exists():
        if final:
            raise LoopError("Independent final requires a completed full evaluation")
        start(stage, baseline, session, output, diagnosis, deadline)
    elif not final and (session / "state.json").exists():
        state = read(session / "state.json")
        if state.get("pending"):
            raise LoopError("Unsettled InferRSI attempt; recovery must not relaunch it")
        if state.get("rounds"):
            return export(stage_name, source_hash, session, receipt, final=False, comparisons=stage.get("comparisons", []), expected_native=expected_native, local_contract=stage.get("local_validation"))
    if final:
        state = read(session / "state.json")
        if state.get("final"):
            return export("final", source_hash, session, receipt, final=True, comparisons=stage.get("comparisons", []), expected_native=expected_native, local_contract=stage.get("local_validation"))
        if state.get("best", {}).get("kind") != "candidate":
            raise LoopError("No eligible candidate was selected for final")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    with resources(stage.get("resources", {}), output) as reserved:
        env.update({key: reserved[key] for key in ("XPU_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES")})
        args = ["campaign", "final" if final else "round", "--session", str(session)]
        if final:
            args += ["--reason", "Candidate passed both screening and full evaluation"]
        else:
            args += ["--candidate", str(candidate), "--hypothesis", hypothesis]
        cli(args, output / ("final-command" if final else "round-command"),
            timeout=max(1, min(stage["timeout_seconds"] + 90, deadline - time.time())), env=env)
    if digest(manifest(candidate)) != source_hash:
        raise LoopError("Candidate changed while being evaluated")
    return export("final" if final else stage_name, source_hash, session, receipt, final=final, comparisons=stage.get("comparisons", []), expected_native=expected_native, local_contract=stage.get("local_validation"))


def verify_local_evidence(session, observed, contract):
    steps = observed.get("execution", {}).get("steps", [])
    seen = set()
    for step in steps:
        side = step["id"].rsplit("-", 1)[-1]
        if side not in ("baseline", "candidate"):
            continue
        value = read(session / observed["path"] / "execution" / step["id"] / "result.json")
        proof = value.get("local_validation", {})
        if proof.get("contract_digest") != digest(contract) or proof.get("weights_verified") is not True:
            raise LoopError("Real-model local validation evidence is missing or stale")
        if step['id'].startswith(('measurement-', 'warmup-')) and contract.get('measurement_plan'):
            import math
            plan = contract['measurement_plan']
            samples = value.get('samples', [])
            if any(value.get(k) != plan[k] for k in ('warmups', 'measurements')) or len(samples) != plan['measurements'] or any(type(x) not in (float, int) or not math.isfinite(x) or x <= 0 for x in samples):
                raise LoopError('Local test did not execute the frozen measurement plan')
        if step["id"].startswith("quality-"):
            if proof.get("numerical_status") == "needs_review":
                return "needs_numerical_review"
            if proof.get("numerical_status") not in ("pass", "fail") or type(value.get("numerical_ok")) is not bool:
                raise LoopError("Missing module numerical comparison")
            if value["numerical_ok"] != (proof["numerical_status"] == "pass"):
                raise LoopError("Conflicting numerical verdict")
            seen.add(side)
    if seen != {"baseline", "candidate"}:
        raise LoopError("Incomplete paired local numerical tests")
    return "verified"


def export(stage, source_hash, session, receipt, *, final, comparisons=(), expected_native=None, local_contract=None):
    state = read(session / "state.json")
    observed = state.get("final") if final else (state.get("rounds") or [None])[-1]
    if not observed:
        raise LoopError("InferRSI did not retain a completed observation")
    assessment = observed["assessment"]
    verdict = assessment.get("verdict", "INCONCLUSIVE")
    if verdict not in ("PASS", "FAIL", "INCONCLUSIVE"):
        raise LoopError("Unknown InferRSI verdict")
    result = {"stage": stage, "verdict": verdict, "reason": assessment.get("reason"),
              "source_hash": source_hash, "assessment": assessment,
              "campaign": str(session), "observation": observed["path"],
              "final": final, "timestamp": time.time(), "model_quality_accepted": False}
    if local_contract:
        try:
            result["local_validation"] = verify_local_evidence(session, observed, local_contract)
            if result["local_validation"] != "verified":
                result.update(verdict="INCONCLUSIVE", reason=result["local_validation"])
        except (LoopError, OSError, KeyError, ValueError, TypeError):
            result.update(verdict="INCONCLUSIVE", reason="local_numerical_or_weight_evidence_missing")
    occupancy = session.parent / "occupancy.json"
    if occupancy.exists():
        result["occupancy"] = read(occupancy)
        if result["occupancy"]["external_activity"] or result["occupancy"]["probe_failed"]:
            result.update(verdict="INCONCLUSIVE", reason="formal_measurement_occupancy_invalid")
    if result["verdict"] == "PASS" and not final and observed.get("selection", {}).get("outcome") != "adopted":
        result.update(verdict="FAIL", reason="candidate_not_better_than_baseline")
    if comparisons and result["verdict"] != "INCONCLUSIVE" and assessment.get("quality_passed") is True and assessment.get("metrics"):
        import math
        import statistics
        gates = []
        try:
            for rule in comparisons:
                values = {"baseline": [], "candidate": []}
                for step in observed["execution"]["steps"]:
                    if not step["id"].startswith("measurement-"):
                        continue
                    side = step["id"].rsplit("-", 1)[-1]
                    value = read(session / observed["path"] / "execution" / step["id"] / "result.json")
                    for key in rule["key"].split("."):
                        value = value[key]
                    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                        raise ValueError("Invalid additional metric")
                    values[side].append(value)
                before, after = (statistics.median(values[x]) for x in ("baseline", "candidate"))
                change = (after / before - 1) * 100
                if rule["direction"] == "minimize":
                    change = -change
                gates.append({**rule, "baseline": before, "candidate": after, "improvement_pct": change,
                              "passed": change >= rule["min_improvement_pct"]})
            result["additional_gates"] = gates
            if result["verdict"] == "PASS" and not all(g["passed"] for g in gates):
                result.update(verdict="FAIL", reason="additional_frozen_gate_not_met")
        except (OSError, ValueError, KeyError, TypeError, statistics.StatisticsError):
            result.update(verdict="INCONCLUSIVE", reason="additional_metric_evidence_missing")
    if expected_native and result['verdict'] != 'INCONCLUSIVE':
        try:
            verify_native_evidence(session, observed, expected_native)
        except (LoopError, OSError, KeyError, ValueError, TypeError):
            result.update(verdict='INCONCLUSIVE', reason='native_build_load_evidence_missing_or_invalid')
    if final and observed.get("candidate_sha256") != state["best"]["sha256"]:
        raise LoopError("Final did not evaluate the selected candidate")
    write(receipt, result)
    write(receipt.with_name("final-metrics.json" if final else "metrics.json"), assessment)
    return result
