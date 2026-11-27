"""Optional target scheduler. Reuse child loops and native assessments; never rewrite them."""
import copy
import math
from pathlib import Path
import subprocess
import time

from . import ablation, evaluator, profiling, reporter
from .common import Busy, LoopError, copy_inputs, digest, manifest, read, write

TERMINAL = {'goal_met', 'goal_not_met', 'pending_review'}
CHILD_DONE = {'screen_only_complete', 'budget_exhausted', 'ablation_complete', 'finalization_incomplete'}


def enabled(config):
    return config.get('objective', {}).get('enabled', False) is True


def validate(config):
    if not enabled(config):
        return
    o = config['objective']
    if config.get('task', 'explore') != 'explore':
        raise LoopError('Objectives wrap exploration, not a standalone ablation')
    for k in ('batch_candidates', 'max_batches'):
        if type(o.get(k)) is not int or o[k] < 1:
            raise LoopError('Objective requires positive ' + k)
    for k in ('batch_seconds', 'verification_seconds', 'final_reserve_seconds'):
        if type(o.get(k)) not in (float, int) or not math.isfinite(o[k]) or o[k] <= 0:
            raise LoopError('Objective requires finite positive ' + k)
    if o['final_reserve_seconds'] >= config['budget']['max_seconds']:
        raise LoopError('Reserve final verification inside the overall budget')
    if type(o.get('ablation_every_batches', 0)) is not int or o.get('ablation_every_batches', 0) < 0:
        raise LoopError('ablation_every_batches must be a nonnegative integer; zero means final only')
    targets = o.get('targets', {})
    if not targets or set(targets) != set(config.get('workload_tasks', [])):
        raise LoopError('Declare one objective for every workload task')
    for task, target in targets.items():
        stages = [target.get('screen_stage'), target.get('validation_stage')]
        for name in stages:
            stage = config['protocols'].get(name, {})
            if stage.get('task') != task or stage.get('direction') not in ('minimize', 'maximize'):
                raise LoopError('Objective stages must explicitly identify task and direction')
        if len({'min_speedup', 'threshold'} & target.keys()) != 1:
            raise LoopError('Each target needs exactly one of min_speedup or threshold')
        value = target.get('min_speedup', target.get('threshold'))
        if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
            raise LoopError('Objective values must be finite and positive')
        if 'min_speedup' in target and value <= 1:
            raise LoopError('Optimization speedup must exceed one')
        if not target.get('workload'):
            raise LoopError('Declare workload identity; freeze its request files inside protocols')
    if config.get('workflow', {}).get('after_explore', 'stop') != 'stop':
        raise LoopError('Objective scheduler owns the exploration/ablation handoff')


def inspect_result(decision, stage, target=None):
    """Target checks use native measured evidence, never local gains or model claims."""
    a = decision.get('assessment', {})
    m = a.get('metrics', {})
    if (decision.get('verdict') == 'INCONCLUSIVE' or decision.get('evidence_verified') is not True
            or a.get('quality_passed') is not True or not m
            or m.get('name') != stage['metric_key'] or m.get('unit') != stage['metric_unit']
            or m.get('direction') != stage['direction']):
        return {'status': 'insufficient_evidence', 'met': False}
    b, c = m.get('baseline'), m.get('candidate')
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (b, c)):
        return {'status': 'insufficient_evidence', 'met': False}
    speedup = b / c if stage['direction'] == 'minimize' else c / b
    met = speedup >= 1
    if target:
        if 'min_speedup' in target:
            met = met and speedup >= target['min_speedup']
            gap = max(0, target['min_speedup'] - speedup)
        else:
            threshold = target['threshold']
            met = met and (c <= threshold if stage['direction'] == 'minimize' else c >= threshold)
            gap = max(0, c-threshold if stage['direction'] == 'minimize' else threshold-c)
    else:
        gap = None
    return {'status': 'measured', 'baseline': b, 'candidate': c, 'unit': m['unit'],
            'speedup': speedup, 'met': met, 'gap': gap, 'quality_passed': True}


def save(config, run, state):
    write(run/'state.json', state)
    children = []
    for p in sorted((run/'batches').glob('*/summary.json')):
        children.append(read(p))
    summary = {'run_id': run.name, 'status': state['status'], 'reason': state.get('reason'),
               'model': config['model']['name'], 'scope': 'objective_target_workloads',
               'knowledge_enabled': config.get('knowledge', {}).get('enabled', True),
               'completed_rounds': sum(c.get('completed_rounds', 0) for c in children),
               'candidate_count': sum(c.get('candidate_count', 0) for c in children),
               'effective_experiments': sum(c.get('effective_experiments', 0) for c in children),
               'candidate_optimizations': [], 'history': [], 'task': 'objective',
               'objective': state['objective'], 'active_run': state['objective'].get('active_run')}
    write(run/'summary.json', summary)
    reporter.enqueue(run)
    return summary


def initial(run):
    baseline = run/'frozen/baseline'
    return {'phase': 'profile', 'batch': 0, 'best_source': str(baseline),
            'best_hash': digest(manifest(baseline)), 'original_hash': digest(manifest(baseline)),
            'factors': [], 'cycles': [], 'active_run': None, 'profile_refreshes': 0,
            'target_results': {}, 'target_met': False}


def _child_config(config):
    child = copy.deepcopy(config)
    child.pop('objective', None)
    child.update(workflow={'after_explore': 'stop'}, knowledge_sources=[],
                 history_files=[], sync={'automatic': False})
    return child


def _start_batch(config, run, state):
    from . import controller
    o, cfg = state['objective'], _child_config(config)
    policy = config['objective']
    remaining = state['deadline'] - time.time() - policy['final_reserve_seconds']
    consumed = sum(read(p).get('candidate_count',0) for p in (run/'batches').glob('*/summary.json'))
    available = config['budget']['max_rounds'] - consumed
    if remaining < 30 or available <= 0 or o['batch'] >= policy['max_batches']:
        o['phase'] = 'verify'
        return
    number = o['batch'] + 1
    child = run/'batches'/f'batch-{number:03d}'
    cfg['project']['path'] = o['best_source']
    cfg['budget'].update(max_rounds=min(available, policy['batch_candidates']), max_seconds=min(remaining, policy['batch_seconds']),
                         deadline=min(state['deadline']-policy['final_reserve_seconds'],time.time()+policy['batch_seconds']))
    cfg['objective_context'] = {'targets': policy['targets'], 'best_hash': o['best_hash'],
        'original_hash': o['original_hash'], 'last_results': o['target_results'],
        'previous_batches': o['cycles'][-10:], 'profile_source': o['diagnosis'].get('source'),
        'note': 'Original target baseline is immutable. This batch starts from the current verified best; prior-batch traces may describe a different source.'}
    cfg['profile']['evidence'] = o['diagnosis']['evidence']
    refreshes = max(0, config.get('profile', {}).get('max_refreshes', 0) - o['profile_refreshes'])
    cfg['profile']['mode'] = 'adaptive' if refreshes and config['profile'].get('mode') == 'adaptive' else 'reuse'
    if cfg['profile']['mode'] == 'adaptive':
        cfg['profile']['max_refreshes'] = refreshes
    # Frozen external corpus survives on; cross-batch feedback belongs to this run even off.
    cfg['own_feedback_directories'] = [str(p.parent) for p in sorted((run/'batches').glob('*/knowledge/catalog.json'))]
    if child.exists():
        controller.verify(child)
    else:
        controller.initialize(cfg, child)
    o.update(batch=number, active_run=str(child), phase='explore')


def _comparison_rows(child):
    return [read(p) for p in sorted((child/'comparisons').glob('*/*/comparison.json'))]


def _choose(config, child, baseline_name='baseline'):
    """Only choose a directly tested bundle, with nonregression in EVERY task."""
    cfg = read(child/'frozen/config.json')
    candidates = {}
    for row in _comparison_rows(child):
        if row['reference'] != baseline_name:
            continue
        stage = cfg['protocols'][row['stage']]
        decision = read(row['decision'])
        value = inspect_result(decision, stage)
        candidates.setdefault(row['candidate'], {})[stage['task']] = value
    eligible = []
    for name, values in candidates.items():
        if set(values) == set(config['objective']['targets']) and all(x['met'] for x in values.values()):
            eligible.append((min(x['speedup'] for x in values.values()), name))
    if not eligible:
        return None
    _, name = max(eligible)
    variant = next(v for v in cfg['ablation']['variants'] if v['name'] == name)
    return {'name': name, 'source': variant['source'], 'source_hash': variant['source_hash'],
            'results': candidates[name], 'enabled': variant['enabled']}


def _verify(config, run, state, phase):
    """A fresh target comparison plus native final; the final must test this exact source."""
    o, rows = state['objective'], {}
    deadline = min(state['deadline'], o.setdefault('phase_deadline', time.time()+config['objective']['verification_seconds']))
    for task, target in config['objective']['targets'].items():
        stage = config['protocols'][target['validation_stage']]
        output = run/'validation'/f'batch-{o["batch"]:03d}'/phase/task
        if deadline-time.time() < 10:
            rows[task] = {'status': 'insufficient_budget', 'met': False}
            continue
        decision = evaluator.assess(target['validation_stage'], stage, run/'frozen/baseline',
            Path(o['best_source']), 'Verify cumulative target against original baseline', output,
            o['diagnosis'], deadline, knowledge_enabled=config.get('knowledge', {}).get('enabled', True))
        value = inspect_result(decision, stage, target)
        if value['met'] and decision['verdict'] == 'PASS':
            final = evaluator.assess(target['validation_stage'], stage, run/'frozen/baseline',
                Path(o['best_source']), 'Independent final confirmation', output, o['diagnosis'], deadline, final=True)
            value = inspect_result(final, stage, target)
            # export() also checks the candidate source hash; never accept a final of baseline.
            value['decision'] = str(output/'final-decision.json')
        else:
            value['met'] = False
            value['decision'] = str(output/'decision.json')
        rows[task] = value
    o['target_results'] = rows
    o['target_met'] = bool(rows) and all(x['met'] for x in rows.values())
    o.pop('phase_deadline', None)
    write(run/'validation'/f'batch-{o["batch"]:03d}'/(phase+'.json'), rows)
    return o['target_met']


def _adopt(config, run, state, batch, selected):
    o = state['objective']
    retained = []
    for name in selected['enabled']:
        number = int(name.split('_')[-1])
        folder = batch/'rounds'/str(number)
        proposal = read(folder/'proposal.json')
        identity = f'b{o["batch"]:03d}_{name}'
        # Earlier batches are prerequisites unless independent applicability is demonstrated.
        required = [x['id'] for x in o['factors']] + [f'b{o["batch"]:03d}_{x}' for x in proposal.get('requires', [])]
        retained.append({'id': identity, 'patch': str(folder/'candidate.patch'),
            'patch_hash': digest((folder/'candidate.patch').read_text()), 'requires': required,
            'hypothesis': proposal['hypothesis'], 'origin': str(folder)})
    o['factors'] += retained
    o.update(best_source=selected['source'], best_hash=selected['source_hash'])


def _start_ablation(config, run, state):
    """Reuse the ablation executor with cumulative patches; preserve dependencies."""
    from . import controller
    o = state['objective']
    child = run/'ablations'/f'batch-{o["batch"]:03d}'
    if not child.exists():
        factors = o['factors']
        names = [x['id'] for x in factors]
        destination = run/'ablation-inputs'/f'batch-{o["batch"]:03d}'
        variants, pairs, exclusions = [], [], []
        sets = [('all', names)]
        if len(names) > 1:
            for factor in names:
                if any(factor in x['requires'] for x in factors):
                    exclusions.append({'factor': factor, 'reason': 'removal_breaks_dependency'})
                else:
                    sets.append(('without_'+factor, [x for x in names if x != factor]))
        for name, included in sets:
            target = destination/name
            receipt = destination/(name+'.json')
            if not receipt.exists():
                if target.exists():
                    # Reconstruct interrupted preparation; no experiment uses this uncommitted variant.
                    import shutil
                    shutil.rmtree(target)
                copy_inputs(run/'frozen/baseline', target, ['.'])
                for factor in factors:
                    if factor['id'] not in included:
                        continue
                    patch = Path(factor['patch']).read_text()
                    if digest(patch) != factor['patch_hash']:
                        raise LoopError('Retained patch changed')
                    checked = subprocess.run(['git','apply','-'], input=patch, text=True, cwd=target, capture_output=True)
                    if checked.returncode:
                        raise LoopError('Cumulative composition requires a new validated repair: '+checked.stderr[-1000:])
                write(receipt, {'source_hash': digest(manifest(target))})
            if digest(manifest(target)) != read(receipt)['source_hash']:
                raise LoopError('Ablation variant changed')
            if name == 'all' and digest(manifest(target)) != o['best_hash']:
                raise LoopError('Cumulative patches do not reconstruct the verified best')
            variants.append({'name': name, 'source': str(target), 'enabled': included})
            pairs.append({'reference': 'baseline', 'candidate': name})
            if name != 'all':
                pairs.append({'reference': 'all', 'candidate': name, 'removed': name[len('without_'):]})
        cfg = _child_config(config)
        cfg.update(task='ablate', profile={'mode':'skip'}, ablation={'design':'explicit',
            'variants': variants, 'comparisons': pairs, 'factors': names,
            'stages': [t['screen_stage'] for t in config['objective']['targets'].values()], 'exclusions': exclusions})
        cfg['project']['path'] = str(run/'frozen/baseline')
        remaining = state['deadline'] - time.time()
        cfg['budget'].update(max_rounds=len(pairs), max_seconds=max(1,min(remaining,config['objective']['verification_seconds'])),
                             deadline=min(state['deadline'], time.time()+config['objective']['verification_seconds']))
        controller.initialize(cfg, child)
    o.update(active_run=str(child), phase='ablate')


def advance(config, run, state):
    from . import controller
    o = state.setdefault('objective', initial(run))
    try:
        if state['status'] in TERMINAL:
            return read(run/'summary.json')
        if digest(manifest(o['best_source'])) != o['best_hash']:
            raise LoopError('Verified best source changed')
        if time.time() >= state['deadline']:
            state.update(status='goal_not_met', reason='overall_budget_exhausted')
            return save(config, run, state)
        phase = o['phase']
        if phase == 'profile':
            o['diagnosis'] = profiling.evidence(config, run, state['deadline'])
            o['phase'] = 'next_batch'
        elif phase == 'next_batch':
            _start_batch(config, run, state)
        elif phase == 'explore':
            child = Path(o['active_run'])
            result = controller.run_loop(child, resume=True)
            if result['status'] not in CHILD_DONE:
                state.update(status=result['status'], reason=result.get('reason'))
                return save(config,run,state)
            child_state = read(child/'state.json')
            cfg = read(child/'frozen/config.json')
            o['diagnosis'] = profiling.evidence(cfg, child, child_state['deadline'])
            o['profile_refreshes'] += len(list((child/'profiling').glob('request-*.json')))
            o['phase'] = 'combine'
        elif phase == 'combine':
            child = Path(o['active_run']); cfg = read(child/'frozen/config.json'); cs = read(child/'state.json')
            cfg['workflow'] = {'after_explore':'ablate','combination_only':True,
                'ablation_stages':[t['screen_stage'] for t in config['objective']['targets'].values()]}
            cs['overall_deadline'] = min(state['deadline'], o.setdefault('phase_deadline', time.time()+config['objective']['verification_seconds']))
            result = ablation.handoff(cfg,child,cs)
            if result and result.get('status') in ('waiting_resources','blocked'):
                state.update(status=result['status'],reason='combination_incomplete')
                return save(config,run,state)
            selected = _choose(config,child/'ablation') if (child/'ablation/frozen/config.json').exists() else None
            if selected:
                _adopt(config,run,state,child,selected)
            feedback = {'batch':o['batch'], 'run':str(child), 'adopted':selected is not None,
                        'source_hash':o['best_hash'], 'short_results':selected['results'] if selected else {},
                        'candidate_count':cs.get('candidate_count',0), 'reason':result.get('reason') if result else 'no_candidates'}
            o['cycles'].append(feedback)
            o.pop('phase_deadline',None)
            o['active_run'] = None
            interval = config['objective'].get('ablation_every_batches',0)
            o['phase'] = 'start_ablation' if selected and interval and o['batch'] % interval == 0 else 'verify'
        elif phase == 'start_ablation':
            if not o['factors']:
                o['phase'] = 'verify'
            else:
                _start_ablation(config,run,state)
        elif phase == 'ablate':
            child = Path(o['active_run']); result = controller.run_loop(child,resume=True)
            if result['status'] not in CHILD_DONE:
                state.update(status=result['status'],reason='ablation_incomplete')
                return save(config,run,state)
            if result['status'] != 'ablation_complete':
                state.update(status='goal_not_met', reason='final_ablation_incomplete')
                return save(config,run,state)
            selected = _choose(config,child)
            if selected:
                o.update(best_source=selected['source'],best_hash=selected['source_hash'],
                         factors=[f for f in o['factors'] if f['id'] in selected['enabled']])
            o.update(active_run=None, last_ablation_batch=o['batch'], phase='confirm' if o.get('confirm_after_ablation') else 'verify')
            o['confirm_after_ablation'] = False
        elif phase in ('verify','confirm'):
            if not o['factors']:
                if o['batch'] >= config['objective']['max_batches'] or time.time() >= state['deadline']-config['objective']['final_reserve_seconds'] or sum(read(p).get('candidate_count',0) for p in (run/'batches').glob('*/summary.json')) >= config['budget']['max_rounds']:
                    state.update(status='goal_not_met',reason='no_verified_candidates_within_budget')
                    return save(config,run,state)
                o['phase'] = 'next_batch'
                state.update(status='ready',reason='continue_exploration')
                return save(config,run,state)
            met = _verify(config,run,state,phase)
            if met:
                if phase != 'confirm' and o.get('last_ablation_batch') != o['batch'] and o['factors']:
                    o.update(phase='start_ablation',confirm_after_ablation=True)
                else:
                    state.update(status='pending_review' if config.get('human_review_required',True) else 'goal_met',
                                 reason='independent_target_checks_passed')
                    return save(config,run,state)
            elif sum(read(p).get('candidate_count',0) for p in (run/'batches').glob('*/summary.json')) >= config['budget']['max_rounds'] or o['batch'] >= config['objective']['max_batches'] or time.time() >= state['deadline']-config['objective']['final_reserve_seconds']:
                state.update(status='goal_not_met',reason='search_budget_exhausted')
                return save(config,run,state)
            else:
                o['phase'] = 'next_batch'
        else:
            raise LoopError('Unknown objective phase')
        state.update(status='ready',reason='objective_'+o['phase'])
    except Busy:
        state.update(status='waiting_resources',reason='objective_devices_busy')
    except (LoopError,OSError,ValueError,KeyError) as exc:
        state.update(status='blocked',reason=str(exc))
    return save(config,run,state)
