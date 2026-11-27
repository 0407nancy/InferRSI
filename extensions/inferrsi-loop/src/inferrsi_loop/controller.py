from __future__ import annotations

import copy
import os
from pathlib import Path
import time

from . import ablation, evaluator, knowledge, local_tests, profiling, reporter, runner, lifecycle
from .common import Busy, LoopError, copy_inputs, digest, lock, manifest, read, sha, write


def validate(config):
    from .configuration import filled
    filled(config)
    project = config["project"]
    if not project.get("include"):
        raise LoopError("Declare project inputs and editable paths")
    if any(Path(p).is_absolute() or ".." in Path(p).parts for p in project.get("editable", ["."])):
        raise LoopError("Editable paths must be project-relative")
    if config["budget"]["max_rounds"] < 1 or config["budget"]["max_seconds"] <= 0:
        raise LoopError("An explicit positive exploration budget is required")
    task = config.get("task", "explore")
    if task not in ("explore", "ablate"):
        raise LoopError("Unknown task type")
    workflow = config.get('workflow', {})
    if workflow.get('after_explore', 'stop') not in ('stop', 'ablate'):
        raise LoopError('workflow.after_explore must be stop or ablate')
    if task == 'explore' and workflow.get('after_explore') == 'ablate':
        if not 0 < workflow.get('ablation_max_seconds', 0) < config['budget']['max_seconds']:
            raise LoopError('Reserve ablation time inside the total run budget')
        stages = workflow.get('ablation_stages', [])
        if not stages or any(x not in config['protocols'] for x in stages):
            raise LoopError('Declare task-specific ablation protocols before exploration')
        tasks = {config['protocols'][x].get('task') for x in stages}
        if not set(config.get('workload_tasks', [])).issubset(tasks):
            raise LoopError('Short E2E ablation must cover all configured tasks separately')
    profiling.validate(config.get("profile", {"mode": "skip"}), task)
    if task == "ablate":
        ablation.validate(config)
    if config.get("exploration", {}).get("validation") == "local_only":
        routes = config.get("screen_routes", [{"protocol": "screen"}])
        for route in routes:
            protocol = config["protocols"][route["protocol"]]
            if protocol.get("local_validation", {}).get("kind") != "local_module" or protocol["quality_key"] != "numerical_ok":
                raise LoopError("Local-only exploration requires a real-model local numerical protocol; E2E fallback is forbidden")
    for name in config["protocols"]:
        stage = config["protocols"][name]
        for field in ("directory", "instructions", "measure_command", "quality_command", "metric_key",
                      "metric_unit", "quality_key", "scope", "direction", "min_improvement_pct",
                      "repetitions", "warmups", "timeout_seconds"):
            if field not in stage:
                raise LoopError(f"Missing {name} protocol field: {field}")
        if stage["min_improvement_pct"] < 0:
            raise LoopError("Improvement gates cannot be negative")


def initialize(config, run, *, document_snapshots=None):
    config = knowledge.prepare_policy(config)
    validate(config)
    run = Path(run).resolve()
    if run.exists():
        raise LoopError("Run directory exists; use resume")
    if not run.name.replace("-", "").replace("_", "").isalnum():
        raise LoopError("Run name must contain letters, digits, hyphens or underscores")
    run.mkdir(parents=True)
    frozen = run / "frozen"
    frozen.mkdir()
    cfg = copy.deepcopy(config)
    cfg.setdefault("profile", {"mode": "skip"})
    cfg["project"].setdefault("editable", ["."])
    cfg["profile"]["input_hashes"] = {arg: sha(arg) for arg in cfg["profile"].get("argv", [])
                                       if Path(arg).is_file() and Path(arg).suffix in (".py", ".sh", ".json")}
    original = Path(cfg["project"]["path"]).resolve()
    if run.is_relative_to(original):
        raise LoopError("Run artifacts must be outside the source project")
    copy_inputs(original, frozen / "baseline", cfg["project"]["include"])
    cfg["project"]["path"] = str(frozen / "baseline")
    for name, stage in cfg["protocols"].items():
        target = frozen / "protocols" / name
        copy_inputs(stage["directory"], target, list(manifest(stage["directory"])))
        instruction = target / "instructions.txt"
        instruction.write_bytes(Path(stage["instructions"]).read_bytes())
        stage.update(directory=str(target), instructions=str(instruction))
    if cfg["profile"].get("directory"):
        original_profile = cfg["profile"]["directory"]
        target = frozen / "profiling"
        copy_inputs(original_profile, target, list(manifest(original_profile)))
        cfg["profile"]["directory"] = str(target)
        cfg["profile"]["argv"] = [arg.replace(original_profile, str(target)) for arg in cfg["profile"]["argv"]]
        cfg["profile"]["input_hashes"] = {name.replace(original_profile, str(target)): value for name, value in cfg["profile"]["input_hashes"].items()}
    if cfg.get("task") == "ablate":
        ablation.freeze(cfg, frozen)
    if cfg["profile"].get("evidence"):
        target = frozen / "profile-evidence.json"
        target.write_bytes(Path(cfg["profile"]["evidence"]).read_bytes())
        cfg["profile"]["evidence"] = str(target)
    history = []
    for i, name in enumerate(cfg.get("history_files", [])):
        target = frozen / f"history-{i}.txt"
        target.write_bytes(Path(name).read_bytes())
        history.append(str(target))
    cfg["history_files"] = history
    if cfg.get('knowledge_sources'):
        cfg['knowledge_directory'] = knowledge.freeze_project(cfg['knowledge_sources'], frozen / 'knowledge',
            document_snapshots, exclude=[run])
    if cfg.get("reference_directory"):
        catalog_path = Path(cfg["reference_directory"]) / "catalog.json"
        if catalog_path.is_file():
            for item in read(catalog_path).get("files", []):
                if item.get("use") == "current_source_semantics_verify_dispatch":
                    if not Path(item["origin"]).is_file() or sha(item["origin"]) != item["sha256"]:
                        raise LoopError("Current reference source changed; refresh reference package: " + item["origin"])
        target = frozen / "references"
        copy_inputs(cfg["reference_directory"], target, list(manifest(cfg["reference_directory"])))
        cfg["reference_directory"] = str(target)
    for i, seed in enumerate(cfg.get('replay_candidates', []), 1):
        source = Path(seed['source'])
        target = frozen / 'replay' / str(i) / 'candidate'
        copy_inputs(source, target, list(manifest(source)))
        seed['source'] = str(target)
        if seed.get('protocol'):
            protocol = target.parent / 'protocol'
            copy_inputs(seed['protocol'], protocol, list(manifest(seed['protocol'])))
            seed['protocol'] = str(protocol)
    write(frozen / "config.json", cfg)
    write(run / "frozen-manifest.json", manifest(frozen))
    write(run / "runtime-manifest.json", manifest(Path(__file__).parent))
    state = {"run_id": run.name, "status": "ready", "round": 1, "completed_rounds": 0,
             "started_at": time.time(), "deadline": time.time() + cfg["budget"]["max_seconds"],
             "history": [], "last_verdict": None, "last_stage": None, "reason": "initialized",
             "current_source": str(frozen / "baseline")}
    state['overall_deadline'] = state['deadline']
    if cfg.get('task','explore') == 'explore' and cfg.get('workflow',{}).get('after_explore') == 'ablate':
        state['deadline'] -= cfg['workflow']['ablation_max_seconds']
    write(run / "state.json", state)
    return run


def verify(run):
    if manifest(Path(run) / "frozen") != read(Path(run) / "frozen-manifest.json"):
        raise LoopError("Frozen inputs or acceptance rules changed")
    if manifest(Path(__file__).parent) != read(Path(run) / "runtime-manifest.json"):
        raise LoopError("Loop runtime changed; resume with the retained runtime version")


def save(run, config, state):
    knowledge.writeback(config, run, state['history'])
    state['candidate_count'] = len(list((Path(run) / 'rounds').glob('*/proposal.json')))
    state['completed_candidates'] = state['completed_rounds']
    measured = {x['round'] for x in state['history'] if x.get('assessment', {}).get('metrics')
                and x.get('evidence_verified', True)
                and x.get('reason') != 'local_numerical_or_weight_evidence_missing'
                and x.get('verdict') in ('PASS', 'FAIL')}
    state['effective_experiments'] = len(measured)
    state['protocol_repairs'] = sum(len(read(p).get('repairs', [])) for p in (Path(run)/'rounds').glob('*/protocol-state.json'))
    state['evidence_rounds'] = len(list((Path(run)/'rounds').glob('*/supplements/*/receipt.json')))
    write(run / "state.json", state)
    summary = {key: state.get(key) for key in (
        "run_id", "status", "completed_rounds", "last_verdict", "last_stage", "reason", "history", "candidate_count", "completed_candidates", "effective_experiments", "protocol_repairs", "evidence_rounds")}
    local_only = config.get("exploration", {}).get("validation") == "local_only"
    summary.update(task=config.get("task", "explore"), model=config["model"]["name"], full_validation_accepted=state["status"] == "accepted",
                   scope="local_module_candidates" if local_only and config.get("task", "explore") == "explore" else config["protocols"].get("short", config["protocols"].get("full", config["protocols"]["screen"]))["scope"],
                   candidate_optimizations=state.get("candidate_optimizations", []))
    summary['knowledge_enabled'] = knowledge.enabled(config)
    summary['ablation'] = state.get('ablation')
    summary['campaign_finals'] = state.get('campaign_finals', [])
    write(run / "candidate-optimizations.json", state.get("candidate_optimizations", []))
    write(run / "summary.json", summary)
    (run / "history.jsonl").write_text("".join(__import__("json").dumps(x, ensure_ascii=False) + "\n" for x in state["history"]))
    reporter.enqueue(run)
    if config.get("sync", {}).get("automatic", False):
        try:
            reporter.sync(run, config["sync"]["document"], config["sync"].get("binary", "lark-cli"))
        except (LoopError, OSError, ValueError) as exc:
            write(run / "sync-receipt.json", {"status": "pending", "reason": type(exc).__name__})


def profile(config, run, deadline):
    return profiling.evidence(config, run, deadline)


def run_loop(run, *, resume=False):
    run = Path(run).resolve()
    with lock(run / ".controller.lock"):
        verify(run)
        config = read(run / "frozen/config.json")
        state = read(run / "state.json")
        if state["status"] in ("accepted", "pending_review", "budget_exhausted", "screen_only_complete", "ablation_complete"):
            return read(run / "summary.json")
        if state["status"] == "blocked" and not resume:
            raise LoopError("Run is blocked; use explicit resume after inspecting the reason")
        try:
            if config["model"].get("transport") != "local_ssh":
                raise LoopError("All model calls must use local_driver.py on the Mac")
            diagnosis = profile(config, run, state["deadline"])
            if config.get("task") == "ablate":
                return ablation.execute(config, run, state, diagnosis)
            maximum = config["budget"]["max_rounds"]
            local_only = config.get("exploration", {}).get("validation") == "local_only"
            for number in range(state["round"], maximum + 1):
                state["round"] = number
                if time.time() >= state["deadline"] - evaluator.final_reserve(run / 'campaigns'):
                    break
                verify(run)
                directory = run / "rounds" / str(number)
                directory.mkdir(parents=True, exist_ok=True)
                state.update(status="planning", reason="model_exploration")
                save(run, config, state)
                context = {"goal": config["goal"], "round": number, "profile": diagnosis,
                           "knowledge_enabled": knowledge.enabled(config),
                           "own_experience": knowledge.own_context(run, config['goal']),
                           "profile_excerpt": profiling.excerpt(diagnosis["evidence"]),
                           "reference_directory": config.get("reference_directory"),
                           "reference_guide": (Path(config["reference_directory"]) / "guide.txt").read_text() if config.get("reference_directory") and (Path(config["reference_directory"]) / "guide.txt").is_file() else "",
                           "history_files": config.get("history_files", []),
                           "history_excerpt": [Path(name).read_text()[:8000] for name in config.get("history_files", [])],
                           "previous_feedback": state["history"][-3:],
                           "editable": config["project"]["editable"],
                           "scope": config.get("planning_scope", config["protocols"]["screen"]["scope"]),
                           "screen_routes": config.get("screen_routes", []),
                           "exploration": config.get("exploration", {}),
                           "local_protocols": {name: {"scope": p["scope"], "min_improvement_pct": p["min_improvement_pct"],
                                                       "contract": p["local_validation"]}
                                               for name, p in config["protocols"].items() if p.get("local_validation")},
                           "native_build_contract_available": any(p.get("native_build_contract") for p in config["protocols"].values())}
                if knowledge.enabled(config) and config.get("knowledge_query"):
                    context["knowledge"] = knowledge.retrieve(config["knowledge_query"], directory / "knowledge")
                if knowledge.enabled(config) and config.get('knowledge_directory'):
                    context['project_knowledge'] = knowledge.project_context(config['knowledge_directory'],
                        config.get('knowledge_query', config['goal']) + ' ' +
                        ' '.join(x.get('hypothesis', '') for x in state['history'][-3:]))
                seeds = config.get('replay_candidates', [])
                if number <= len(seeds) and not (directory / 'proposal.json').exists():
                    seed = seeds[number-1]
                    copy_inputs(seed['source'], directory / 'candidate', list(manifest(seed['source'])))
                    proposal = {'hypothesis': seed['hypothesis'], 'rationale': seed.get('rationale',seed['hypothesis']), 'source_hash': digest(manifest(directory / 'candidate')),
                                'replay_origin': seed['origin'], 'model_execution_host': 'retained_candidate'}
                    write(directory / 'context.json', context)
                    write(directory / 'proposal.json', proposal)
                    (directory / 'candidate.patch').write_text(seed.get('patch', 'Retained candidate; see replay_origin'))
                if not (directory / "proposal.json").exists():
                    write(directory / "context.json", context)
                    state.update(status="awaiting_local_proposal", reason="local_codex_required")
                    save(run, config, state)
                    return read(run / "summary.json")
                proposal = read(directory / "proposal.json")
                if digest(manifest(directory / "candidate")) != proposal["source_hash"]:
                    raise LoopError("Retained candidate changed")
                candidate = directory / "candidate"
                # A local candidate is always an independent patch against the frozen
                # baseline; combinations are measured later by the ablation task.
                for stage_name in (("screen",) if local_only else ("screen", "full", "final")):
                    if stage_name != "screen" and (not config.get("enable_full", True) or
                            number < config.get("min_rounds_before_full", 1)):
                        break
                    verify(run)
                    state.update(status="evaluating", last_stage=stage_name, reason="waiting_for_measured_results")
                    save(run, config, state)
                    protocol_name = "full" if stage_name == "final" else stage_name
                    protocol = None
                    if stage_name == "screen" and local_only and config.get("screen_routes") is not None:
                        protocol_name, protocol, changed = local_tests.selected(config, run, candidate)
                        if protocol is None:
                            request_value = local_tests.request(config, run, state, directory, changed)
                            if lifecycle.enabled(config) and lifecycle.cursor(directory)['status'] == 'exhausted':
                                local_tests.unavailable(config, run, directory, {'request_digest': digest(request_value),
                                    'reason': 'Protocol repair budget exhausted; all version evidence retained'}, state)
                                break
                            seed = seeds[number-1] if number <= len(seeds) else {}
                            if seed.get('protocol') and not (directory / 'replay-protocol-submitted.json').exists():
                                import base64
                                payload = {'request_digest': digest(request_value), 'summary': 'Replay original failed protocol unchanged',
                                    'files': {n: base64.b64encode((Path(seed['protocol'])/n).read_bytes()).decode()
                                              for n in manifest(seed['protocol'])}}
                                local_tests.register(config, run, directory, payload)
                                write(directory / 'replay-protocol-submitted.json', {'origin': seed['protocol']})
                                state.update(status='ready', reason='replay_protocol_preflight_pending')
                            save(run, config, state)
                            return read(run / "summary.json")
                    elif stage_name == "screen" and config.get("screen_routes"):
                        from .planner import allowed
                        before, after = manifest(run / "frozen/baseline"), manifest(candidate)
                        changed = {n for n in before.keys() | after.keys() if before.get(n) != after.get(n)}
                        protocol_name = next((route["protocol"] for route in config["screen_routes"] if all(allowed(n, route["paths"]) for n in changed)), None)
                        if protocol_name not in config["protocols"]:
                            raise LoopError("No local test covers this candidate; prepare a real-weight numerical/performance protocol in a new task. E2E fallback is forbidden")
                    protocol = protocol or config["protocols"][protocol_name]
                    request = {"stage": stage_name, "candidate": str(candidate),
                               "source_hash": proposal["source_hash"], "protocol_digest": digest(protocol)}
                    write(directory / "eval-request.json", request)
                    final = stage_name == "final"
                    output = directory / "evaluation" / ("full" if final else stage_name)
                    if final:
                        output = Path(state["full_evaluation"])
                        result = evaluator.assess(stage_name, protocol, run / "frozen/baseline", candidate,
                                                 proposal["hypothesis"], output, diagnosis, state["deadline"], final=True)
                    else:
                        for attempt in range(1, config["budget"].get("max_infra_retries", 1) + 2):
                            revision = read(directory / 'local-test-receipt.json').get('protocol_version', 0) if (directory / 'local-test-receipt.json').exists() else 0
                            attempt_output = output / (f'v{revision:02d}-{attempt}' if revision else str(attempt))
                            result = evaluator.assess(stage_name, protocol, run / "frozen/baseline", candidate,
                                                     proposal["hypothesis"], attempt_output, diagnosis, state["deadline"],
                                                     campaign_root=run / 'campaigns', round_limit=maximum,
                                                     knowledge_enabled=knowledge.enabled(config))
                            if result["verdict"] != "INCONCLUSIVE" or result["reason"] == "needs_numerical_review" or time.time() >= state["deadline"] or (local_only and lifecycle.enabled(config)):
                                if stage_name == "full" and result["verdict"] == "PASS":
                                    state["full_evaluation"] = str(attempt_output)
                                break
                    state.update(last_verdict=result["verdict"])
                    record = {"round": number, "hypothesis": proposal["hypothesis"],
                              "stage": stage_name, "protocol": protocol_name, "verdict": result["verdict"],
                              "reason": result["reason"], "source_hash": proposal["source_hash"],
                              "assessment": result["assessment"]}
                    record.update(campaign=result['campaign'], observation=result['observation'],
                                  evidence_verified=result.get('evidence_verified', True),
                                  decision=str(attempt_output / 'decision.json') if not final else str(output / 'final-decision.json'),
                                  scope=protocol['scope'], coverage=protocol.get('local_validation', {}).get('coverage'))
                    record['protocol_version'] = lifecycle.cursor(directory)['version'] if lifecycle.enabled(config) else 0
                    if not any(x["round"] == number and x["stage"] == stage_name and x.get('protocol_version', 0) == record['protocol_version'] for x in state["history"]):
                        state["history"].append(record)
                    write(directory / "feedback.json", record)
                    if result["verdict"] == "INCONCLUSIVE" or not result.get('evidence_verified', True):
                        if local_only and result["reason"] == "needs_numerical_review":
                            break
                        if local_only and lifecycle.enabled(config) and (directory / 'local-test-receipt.json').exists():
                            if lifecycle.revise(config, directory, 'evaluation_protocol_failure', lifecycle.logs(attempt_output)):
                                local_tests.request(config, run, state, directory, changed)
                                save(run, config, state)
                                return read(run / 'summary.json')
                            state['reason'] = 'Protocol repair budget exhausted'
                            break
                        raise LoopError("Incomplete evaluation evidence; inspect before retry")
                    if result["verdict"] == "FAIL" or not result.get('evidence_verified', True):
                        break
                    if local_only:
                        item = {"round": number, "hypothesis": proposal["hypothesis"],
                                "status": "candidate_optimization", "source": str(candidate),
                                "source_hash": proposal["source_hash"], "baseline_hash": digest(manifest(run / "frozen/baseline")),
                                "patch": str(directory / "candidate.patch"), "protocol": protocol_name,
                                "scope": protocol["scope"], "assessment": result["assessment"], "campaign":result["campaign"],
                                "decision": str(attempt_output / "decision.json"), "e2e_validated": False}
                        candidates = state.setdefault("candidate_optimizations", [])
                        if not any(x["round"] == number for x in candidates):
                            candidates.append(item)
                        continue
                    if stage_name == "full" or (stage_name == "screen" and (
                            not config.get("enable_full", True) or number < config.get("min_rounds_before_full", 1))):
                        state["current_source"] = str(candidate)
                    if final:
                        state.update(status="pending_review" if config.get("human_review_required", True) else "accepted",
                                     completed_rounds=number, reason="independent_final_passed")
                        save(run, config, state)
                        return read(run / "summary.json")
                state.update(completed_rounds=number, round=number + 1)
                save(run, config, state)
            if local_only:
                state['campaign_finals'] = evaluator.finalize_all(run / 'campaigns', state['deadline'])
            incomplete = [x for x in state.get('campaign_finals',[]) if x.get('status') == 'incomplete' and x.get('reason') == 'final_budget_exhausted']
            for item in state.get('candidate_optimizations',[]):
                item['eligible_for_ablation'] = False
                for result in state.get('campaign_finals',[]):
                    final = result.get('final') or {}
                    if item.get('campaign') == result['campaign'] and item['source_hash'] == result.get('source_hash'):
                        item['native_final_verdict'] = final['assessment']['verdict']
                        item['eligible_for_ablation'] = item['native_final_verdict'] == 'PASS' and result.get('evidence_verified',True)
            evaluator.confirm_remaining_candidates(run, state, state['deadline'])
            state['ablation'] = ablation.handoff(config, run, state)
            if state['ablation'] and state['ablation'].get('status') in ('blocked','waiting_resources'):
                raise Busy('Ablation resources unavailable') if state['ablation']['status'] == 'waiting_resources' else LoopError('Ablation incomplete; inspect retained child run')
            if incomplete:
                state.update(status='finalization_incomplete',reason='partial_finals_completed')
                save(run,config,state)
                return read(run/'summary.json')
            state.update(status="screen_only_complete" if (local_only or not config.get("enable_full", True)) and state["completed_rounds"] == maximum
                         else "budget_exhausted", reason="declared_search_budget_reached")
        except Busy:
            state.update(status="waiting_resources", reason="no_idle_reserved_devices")
        except (LoopError, OSError, ValueError, KeyError) as exc:
            state.update(status="blocked", reason=str(exc))
        save(run, config, state)
        return read(run / "summary.json")
