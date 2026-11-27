"""Fixed source variants and explicit paired contrasts, reusing InferRSI."""
from pathlib import Path
import re
import copy
import math
import time

from . import evaluator
from .common import LoopError, copy_inputs, digest, manifest, read, write
from .planner import allowed


def stages(plan):
    return plan.get('stages', [plan.get('stage', 'full')])


def normalize(plan):
    plan = copy.deepcopy(plan)
    if plan.get('design', 'explicit') not in ('explicit', 'leave_one_out'):
        raise LoopError('Unknown ablation design; supply explicit contrasts for other designs')
    if plan.get('design') == 'leave_one_out':
        factors = plan.get('factors', [])
        if not factors or len(set(factors)) != len(factors):
            raise LoopError('Leave-one-out requires unique factor names')
        variants = {'baseline': set()}
        for variant in plan['variants']:
            enabled = variant.get('enabled')
            if not isinstance(enabled, list) or not set(enabled).issubset(factors):
                raise LoopError('Each variant must declare its enabled factors')
            variants[variant['name']] = set(enabled)
        def find(enabled):
            matches = [name for name, value in variants.items() if value == enabled]
            if len(matches) != 1:
                raise LoopError('Provide exactly one source variant for factor set: ' + str(sorted(enabled)))
            return matches[0]
        full = find(set(factors))
        pairs = [{'reference': 'baseline', 'candidate': full}]
        pairs += [{'reference': full, 'candidate': find(set(factors) - {factor}), 'removed': factor} for factor in factors]
        if plan.get('comparisons') and plan['comparisons'] != pairs:
            raise LoopError('Explicit contrasts disagree with leave-one-out design')
        plan['comparisons'] = pairs
    return plan


def contribution(record, plan):
    # Without-X is the denominator: benefit of enabling X in the full bundle.
    if record.get('removed') and record['measured']:
        metric = record['assessment']['metrics']
        full, without = metric['baseline'], metric['candidate']
        gain = (1 - full / without) * 100 if metric['direction'] == 'minimize' else (full / without - 1) * 100
        record['marginal_gain_pct'] = gain
        record['marginal_basis'] = 'Full bundle versus the same bundle without this factor; denominator is without-factor metric'
        threshold = plan.get('min_gain_pct')
        record['passes_selection_threshold'] = None if threshold is None else gain >= threshold
    return record


def validate(config):
    plan = normalize(config['ablation'])
    names = [v['name'] for v in plan['variants']]
    if len(set(names)) != len(names) or any(not re.fullmatch(r'[A-Za-z0-9_-]+', n) or n == 'baseline' for n in names):
        raise LoopError('Ablation variant names must be unique; baseline is reserved')
    names = set(names) | {'baseline'}
    pairs = plan['comparisons']
    if not pairs or len(pairs) > config['budget']['max_rounds']:
        raise LoopError('Declare a nonempty matrix within the run comparison budget')
    for pair in pairs:
        if pair['reference'] not in names or pair['candidate'] not in names or pair['reference'] == pair['candidate']:
            raise LoopError('Invalid ablation contrast')
    selected = stages(plan)
    if not selected or len(set(selected)) != len(selected) or any(s not in config['protocols'] for s in selected):
        raise LoopError('Declare supported, unique ablation measurement stages')
    if plan.get('selection_stage', selected[-1]) not in selected:
        raise LoopError('Selection stage must be one of the measured stages')
    threshold = plan.get('min_gain_pct')
    if threshold is not None and (type(threshold) not in (int, float) or not math.isfinite(threshold) or threshold < 0):
        raise LoopError('min_gain_pct must be a nonnegative finite percentage or null')


def freeze(config, frozen):
    config['ablation'] = normalize(config['ablation'])
    before = manifest(frozen / 'baseline')
    for variant in config['ablation']['variants']:
        target = frozen / 'variants' / variant['name']
        after = copy_inputs(variant['source'], target, config['project']['include'])
        changed = {n for n in before.keys() | after.keys() if before.get(n) != after.get(n)}
        if any(not allowed(n, config['project']['editable']) for n in changed):
            raise LoopError('Ablation source modifies files outside the declared scope')
        variant.update(source=str(target), source_hash=digest(after))


def execute(config, run, state, diagnosis):
    from .controller import save
    plan = config['ablation']
    sources = {'baseline': str(run / 'frozen/baseline'), **{v['name']: v['source'] for v in plan['variants']}}
    for number, pair in enumerate(plan['comparisons'], 1):
        if number <= state['completed_rounds']:
            continue
        for stage_name in stages(plan):
            if time.time() >= state['deadline']:
                state.update(status='budget_exhausted', reason='declared_ablation_budget_reached')
                save(run, config, state)
                return read(run / 'summary.json')
            state.update(status='evaluating', round=number, last_stage=stage_name, reason='measuring_fixed_contrast')
            save(run, config, state)
            folder = run / 'comparisons' / f'{number:03d}' / stage_name
            decision = evaluator.assess(stage_name, config['protocols'][stage_name], Path(sources[pair['reference']]),
                                        Path(sources[pair['candidate']]), f"{pair['candidate']} versus {pair['reference']}",
                                        folder, diagnosis, state['deadline'])
            assessment = decision['assessment']
            measurable = assessment.get('quality_passed') is True and bool(assessment.get('metrics')) and decision['verdict'] != 'INCONCLUSIVE'
            record = {**pair, 'round': number, 'stage': stage_name, 'verdict': decision['verdict'],
                      'reason': decision['reason'], 'assessment': assessment, 'measured': measurable,
                      'reference_hash': digest(manifest(sources[pair['reference']])),
                      'source_hash': digest(manifest(sources[pair['candidate']])), 'decision': str(folder / 'decision.json')}
            contribution(record, plan)
            write(folder / 'comparison.json', record)
            if decision['verdict'] == 'INCONCLUSIVE':
                raise LoopError('Ablation evidence incomplete; retain this contrast for inspection')
            if not any(r['round'] == number and r['stage'] == stage_name for r in state['history']):
                state['history'].append(record)
            state['last_verdict'] = decision['verdict']
            save(run, config, state)
            if assessment.get('quality_passed') is not True:
                write(folder / 'skipped-stages.json', {'reason': 'quality_check_failed',
                      'stages': stages(plan)[stages(plan).index(stage_name) + 1:]})
                break
        state.update(completed_rounds=number)
        save(run, config, state)
    state.update(status='ablation_complete', reason='all_declared_contrasts_evaluated')
    save(run, config, state)
    return read(run / 'summary.json')
