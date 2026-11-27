from __future__ import annotations

import os
from pathlib import Path
import shlex
import sys
import time

from .common import LoopError, copy_inputs, digest, manifest, parse_stdout, read, run_command, sha, write
from .runner import resources


def cli(arguments, directory, *, timeout, env=None):
    result = run_command([sys.executable, "-m", "inferrsi", *arguments], Path.cwd(), directory,
                         timeout, env=env)
    if result["status"] == "completed":
        return parse_stdout(directory)
    # Native status uses exit 2 for a valid blocked state. It is evidence,
    # not a transport failure; do not accept arbitrary error-shaped JSON.
    if (result["status"] == "error" and result.get("returncode") == 2
            and arguments[:2] == ["campaign", "status"]):
        value = parse_stdout(directory)
        if (value.get("state") == "blocked" and value.get("next_action") == "blocked"
                and value.get("can_finalize") is False and "pending" in value
                and isinstance(value.get("session"), str)
                and type(value.get("completed_rounds")) is int
                and isinstance(value.get("latest_observation"), dict)
                and value["latest_observation"].get("assessment", {}).get("verdict") == "INCONCLUSIVE"):
            return value
    # An INCONCLUSIVE assessment deliberately exits nonzero. Only accept it
    # when the CLI returned a retained observation, never an error-shaped JSON.
    if result["status"] == "error" and arguments[:2] in (["campaign", "round"], ["campaign", "final"]):
        value = parse_stdout(directory)
        observation = value.get("latest_observation") or value.get("final")
        if isinstance(observation, dict) and observation.get("assessment", {}).get("verdict") == "INCONCLUSIVE":
            return value
    raise LoopError("InferRSI command failed; inspect its retained logs")


def start(stage, baseline, session, output, diagnosis, deadline, *, round_limit=1, knowledge_enabled=True):
    remaining = deadline - time.time()
    if remaining < 10:
        raise LoopError("No evaluation budget remains")
    if stage.get('local_validation'):
        # The check is frozen INSIDE the native protocol, so native assessment owns failure semantics.
        import copy
        original = stage
        stage = copy.deepcopy(stage)
        target = output / 'guarded-protocol'
        if not target.exists():
            copy_inputs(original['directory'], target, list(manifest(original['directory'])))
            for name in ('_evidence_gate.py', '_evidence_gate.json'):
                if (target / name).exists():
                    raise LoopError('Reserved native evidence-gate filename')
            (target / '_evidence_gate.py').write_bytes((Path(__file__).parent/'data/evidence_gate.py').read_bytes())
            write(target / '_evidence_gate.json', {k: original[k] for k in ('measure_command','quality_command','local_validation')})
        stage['directory'] = str(target)
        for action in ('measure','quality'):
            stage[action+'_command'] = ['{python}', '{protocol}/_evidence_gate.py', '--action', action,
                '--candidate','{candidate}','--baseline','{baseline}','--result','{result}','--python','{python}']
    args = ["campaign", "start", "--repo", str(baseline), "--protocol", str(stage["directory"]),
            "--instructions", str(stage["instructions"]), "--session", str(session),
            "--rounds", str(round_limit), "--round-mode", "up_to", "--max-minutes", str(remaining / 60),
            "--evaluation-timeout-seconds", str(min(stage["timeout_seconds"], remaining)),
            "--final-reserve-seconds", str(min(stage.get("final_reserve_seconds", stage["timeout_seconds"]), remaining / 3)),
            "--condition", "local-codex-v2", "--knowledge", "on" if knowledge_enabled else "off"]
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


def campaign_key(stage, baseline, knowledge_enabled):
    settings = {k: v for k, v in stage.items() if k not in ('directory', 'instructions')}
    return digest({'baseline': manifest(baseline), 'protocol': manifest(stage['directory']),
                   'settings': settings, 'knowledge': knowledge_enabled})[:24]


def status(session, output):
    """Read through the native integrity/state checks, never mutate state.json."""
    cli(['campaign', 'status', '--session', str(session)], output, timeout=60)
    return read(session / 'state.json')


def assess(stage_name, stage, baseline, candidate, hypothesis, output, diagnosis, deadline, *,
           final=False, campaign_root=None, round_limit=1, knowledge_enabled=True):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    receipt = output / ('final-decision.json' if final else 'decision.json')
    source_hash = digest(manifest(candidate))
    native = native_change(baseline, candidate)
    if native and not stage.get('native_build_contract'):
        raise LoopError('Native edits require a frozen rebuild-and-load protocol')
    if stage.get('comparisons'):
        raise LoopError('Use task-specific native campaigns for independent metrics, not external gates')
    expected_native = {'baseline': digest(manifest(baseline)), 'candidate': source_hash} if native else None
    if receipt.exists():
        previous = read(receipt)
        if previous['source_hash'] != source_hash:
            raise LoopError('Candidate differs from retained evaluation')
        return previous
    binding = output / 'campaign-ref.json'
    if binding.exists():
        reference = read(binding)
        if reference['source_hash'] != source_hash:
            raise LoopError('In-flight candidate identity changed')
        session = Path(reference['session'])
    else:
        if campaign_root is None:
            session = output / 'campaign'
        else:
            family = Path(campaign_root) / campaign_key(stage, baseline, knowledge_enabled)
            sessions = sorted(p for p in family.glob('session-*') if p.is_dir() and (p / 'state.json').is_file())
            session = sessions[-1] if sessions else family / 'session-0001'
            if sessions:
                old = status(session, output / 'native-status')
                if old.get('pending'):
                    raise LoopError('Native attempt is in flight; recover its original request first')
                if old['state'] != 'active':
                    session = family / ('session-%04d' % (len(sessions) + 1))
        if not session.exists():
            if final:
                raise LoopError('Final needs a retained campaign')
            start(stage, baseline, session, output, diagnosis, deadline,
                  round_limit=round_limit, knowledge_enabled=knowledge_enabled)
            write(session.parent / (session.name + '-adapter.json'),
                  {'stage': stage, 'name': stage_name, 'baseline': str(baseline), 'deadline': deadline})
        control = status(session, output / 'native-status')
        reference = {'session': str(session), 'source_hash': source_hash,
                     'round_index': len(control.get('rounds', [])), 'knowledge_enabled': knowledge_enabled}
        write(binding, reference)
    control = status(session, output / 'native-status')
    if control.get('pending'):
        pending = control['pending']
        evidence = session / pending['path'] / 'execution/execution.json'
        if not evidence.is_file():
            raise LoopError('Native execution is unresolved; inspect owned process before recovery')
        if read(evidence).get('status') in ('running', 'pending'):
            raise LoopError('Native execution is still running; wait for its retained receipt')
        cli(['campaign', 'recover', '--session', str(session)], output / 'native-recovery', timeout=60)
        control = status(session, output / 'native-status')
    index = reference['round_index']
    completed = control.get('final') if final else (control.get('rounds', [])[index] if len(control.get('rounds', [])) > index else None)
    if completed is None:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        with resources(stage.get('resources', {}), output) as reserved:
            env.update({k: reserved[k] for k in ('XPU_VISIBLE_DEVICES', 'CUDA_VISIBLE_DEVICES')})
            args = ['campaign', 'final' if final else 'round', '--session', str(session)]
            args += ['--reason', 'Declared search completed; independent native retest'] if final else ['--candidate', str(candidate), '--hypothesis', hypothesis]
            cli(args, output / ('final-command' if final else 'round-command'),
                timeout=max(1, min(stage['timeout_seconds'] + 90, deadline - time.time())), env=env)
    if digest(manifest(candidate)) != source_hash:
        raise LoopError('Candidate changed during evaluation')
    return export('final' if final else stage_name, source_hash, session, receipt, final=final,
                  expected_native=expected_native, local_contract=stage.get('local_validation'),
                  round_index=index, occupancy=output / 'occupancy.json')


def final_reserve(root):
    total = 0
    for adapter in Path(root).glob('*/session-*-adapter.json'):
        session = adapter.with_name(adapter.name[:-len('-adapter.json')])
        state = read(session / 'state.json')
        if state['state'] == 'active' and not state.get('final') and state.get('rounds'):
            stage = read(adapter)['stage']
            total += max(10, stage.get('final_reserve_seconds', stage['timeout_seconds']))
    return total


def finalize_all(root, deadline):
    """Native final per compatible campaign; receipts are projections, never verdict rewrites."""
    rows = []
    for adapter in sorted(Path(root).glob('*/session-*-adapter.json')):
        session = adapter.with_name(adapter.name[:-len('-adapter.json')])
        folder = session.parent / (session.name + '-final')
        cfg = read(adapter)
        state = status(session, folder / 'status')
        if state.get('pending'):
            pending = session / state['pending']['path'] / 'execution/execution.json'
            if pending.is_file() and read(pending).get('status') not in ('running', 'pending'):
                cli(['campaign', 'recover', '--session', str(session)], folder / 'recover', timeout=60)
                state = status(session, folder / 'status')
            else:
                raise LoopError('Final is in flight; recover its original execution before continuing')
        if state.get('final'):
            rows.append({'campaign': str(session), 'final': state['final'],
                         'source_hash': read(folder/'decision.json').get('source_hash') if (folder/'decision.json').exists() else None,
                         'evidence_verified': read(folder/'decision.json').get('evidence_verified',False) if (folder/'decision.json').exists() else False})
            continue
        if state['state'] != 'active' or not state.get('rounds'):
            rows.append({'campaign': str(session), 'status': 'incomplete', 'reason': state['state']})
            continue
        if time.time() >= deadline or time.time() >= state['deadline']:
            rows.append({'campaign': str(session), 'status': 'incomplete', 'reason': 'final_budget_exhausted'})
            continue
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        with resources(cfg['stage'].get('resources', {}), folder) as reserved:
            env.update({k: reserved[k] for k in ('XPU_VISIBLE_DEVICES', 'CUDA_VISIBLE_DEVICES')})
            cli(['campaign', 'final', '--session', str(session), '--reason', 'Search phase finished'],
                folder / 'command', timeout=max(1, min(cfg['stage']['timeout_seconds'] + 90, deadline-time.time())), env=env)
        state = status(session, folder / 'status')
        selected = selected_source_hash(session, state)
        decision = export('final', selected, session, folder / 'decision.json', final=True,
                          local_contract=cfg['stage'].get('local_validation'), occupancy=folder / 'occupancy.json')
        rows.append({'campaign': str(session), 'final': state.get('final'), 'decision': str(folder / 'decision.json'),
                     'evidence_verified': decision['evidence_verified'], 'source_hash': selected})
    return rows


def confirm_remaining_candidates(run, state, deadline):
    """Native best is one candidate; independently confirm other passing ideas."""
    for item in state.get('candidate_optimizations', []):
        if 'native_final_verdict' in item:
            continue
        item['eligible_for_ablation'] = False
        folder = Path(run)/'candidate-finals'/str(item['round'])
        if time.time()+10 >= deadline:
            item['final_reason'] = 'final_budget_exhausted'
            continue
        session = Path(item['campaign'])
        adapter = read(session.parent/(session.name+'-adapter.json'))
        stage = adapter['stage']
        try:
            decision = assess(item['protocol'], stage, Path(run)/'frozen/baseline',
                Path(item['source']), item['hypothesis'], folder,
                {'bottleneck':item['hypothesis'],'evidence':item['decision']}, deadline,
                knowledge_enabled=read(Path(run)/'frozen/config.json').get('knowledge',{}).get('enabled',True))
            if decision['verdict'] == 'PASS' and decision.get('evidence_verified') is True:
                decision = assess(item['protocol'], stage, Path(run)/'frozen/baseline',
                    Path(item['source']), item['hypothesis'], folder,
                    {'bottleneck':item['hypothesis'],'evidence':item['decision']}, deadline, final=True)
                item['native_final_verdict'] = decision['verdict']
                item['eligible_for_ablation'] = decision['verdict']=='PASS' and decision.get('evidence_verified') is True
            item['final_reason'] = decision.get('reason')
            item['final_decision'] = str(folder/('final-decision.json' if decision.get('final') else 'decision.json'))
        except (LoopError, OSError, ValueError, KeyError) as exc:
            item['final_reason'] = str(exc)
            write(folder/'incomplete.json', {'reason':str(exc),'candidate_hash':item['source_hash']})


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


def case_results(session, observed, contract):
    import statistics
    result = []
    for case in contract.get('cases', []):
        row = {'id':case['id'],'tasks':[t for t,c in contract.get('coverage',{}).items() if case['id'] in c.get('cases',[])],
               'timing':{},'numerical':{}}
        for step in observed.get('execution',{}).get('steps',[]):
            side = step['id'].rsplit('-',1)[-1]
            if side not in ('baseline','candidate'):
                continue
            path = session/observed['path']/'execution'/step['id']/'result.json'
            if not path.exists():
                continue
            items = read(path).get('cases',[])
            if not isinstance(items,list):
                continue
            entry = next((x for x in items if x.get('id')==case['id']),None)
            if not entry:
                continue
            if step['id'].startswith('measurement-') and entry.get('timed',True):
                row['timing'].setdefault(side,[]).extend(entry.get('samples',[]))
            if step['id'].startswith('quality-'):
                row['numerical'][side] = entry
        if all(row['timing'].get(x) for x in ('baseline','candidate')):
            row['means'] = {x:statistics.mean(row['timing'][x]) for x in ('baseline','candidate')}
        result.append(row)
    return result


def selected_source_hash(session, state):
    # Native sha256 uses a different encoding. Compare retained bytes with the
    # adapter's own manifest format; never equate unrelated hash formats.
    from .common import safe_child
    best = state['best']
    source = safe_child(session, best['path'])
    if not source.is_dir():
        raise LoopError('Missing native selected source snapshot')
    return digest(manifest(source))


def export(stage, source_hash, session, receipt, *, final, comparisons=(), expected_native=None, local_contract=None, round_index=None, occupancy=None):
    state = read(session / "state.json")
    observed = state.get("final") if final else (state.get("rounds") or [None])[-1 if round_index is None else round_index]
    if not observed:
        raise LoopError("InferRSI did not retain a completed observation")
    assessment = observed["assessment"]
    verdict = assessment.get("verdict", "INCONCLUSIVE")
    if verdict not in ("PASS", "FAIL", "INCONCLUSIVE"):
        raise LoopError("Unknown InferRSI verdict")
    result = {"stage": stage, "verdict": verdict, "reason": assessment.get("reason"),
              "source_hash": source_hash, "assessment": assessment,
              "campaign": str(session), "observation": observed["path"],
              "final": final, "timestamp": time.time(), "model_quality_accepted": False, "evidence_verified": True,
              "selection": observed.get("selection"), "native_verdict": verdict}
    if local_contract:
        result['cases'] = case_results(session, observed, local_contract)
        result['coverage'] = local_contract.get('coverage', {})
        try:
            result["local_validation"] = verify_local_evidence(session, observed, local_contract)
            if result["local_validation"] != "verified":
                result.update(evidence_verified=False, reason=result["local_validation"])
        except (LoopError, OSError, KeyError, ValueError, TypeError):
            result.update(evidence_verified=False, reason="local_numerical_or_weight_evidence_missing")
    occupancy = occupancy or session.parent / "occupancy.json"
    if occupancy.exists():
        result["occupancy"] = read(occupancy)
        if result["occupancy"]["external_activity"] or result["occupancy"]["probe_failed"]:
            result.update(evidence_verified=False, reason="formal_measurement_occupancy_invalid")
    if expected_native and result['verdict'] != 'INCONCLUSIVE':
        try:
            verify_native_evidence(session, observed, expected_native)
        except (LoopError, OSError, KeyError, ValueError, TypeError):
            result.update(evidence_verified=False, reason='native_build_load_evidence_missing_or_invalid')
    if final and (observed.get("candidate_sha256") != state["best"]["sha256"]
                  or selected_source_hash(session, state) != source_hash):
        raise LoopError("Final did not evaluate the selected candidate")
    write(receipt, result)
    write(receipt.with_name("final-metrics.json" if final else "metrics.json"), assessment)
    return result
