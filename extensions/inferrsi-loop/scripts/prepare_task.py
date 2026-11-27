"""Translate a reviewed prompt plan into a new config; never mutate old runs."""
import argparse
import copy
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from inferrsi_loop.common import LoopError, copy_inputs, manifest, project_inputs, read, sha, write
from inferrsi_loop.ablation import normalize
from inferrsi_loop.controller import validate
from inferrsi_loop.configuration import filled

LEGACY = ('run.py', 'xprof_run.py', 'server.py', 'xprof_request.py',
          'analyze_torch_trace.py', 'analyze_stages.py', 'analyze_xprof.py', 'audit_device_timeline.py')


def local_contract(spec, rule=None):
    files = {}
    for entries in spec['captures'].values():
        for capture in entries:
            path = Path(capture['path'])
            # These artifacts came from RowParallelLinear.forward's self.weight,
            # not from a freshly initialized layer. Retain the capture producer
            # and launch metadata as provenance alongside the tensor hashes.
            files[str(path)] = dict(capture, role='weight_capture')
            for evidence in (path.parent / 'launch.json', path.parent / 'request.json',
                             path.parent.parent.parent / 'projection_capture_server.py'):
                files[str(evidence)] = {'path': str(evidence), 'sha256': sha(evidence), 'role': 'capture_provenance'}
    return {'kind': 'local_module', 'module': 'h3_tp2_mlp_projection', 'weight_origin': 'model_capture',
            'paths': ['vllm_omni_kunlun/models/minimax_h3/projection_pipeline.py'],
            'input_policy': 'fixed_real_capture', 'evidence_files': list(files.values()),
            'numerical_rule': rule or {'mode': 'exact_or_review',
                'basis': 'Exact captured-output equivalence may pass; nonzero error needs an applicable module rule, never a universal tolerance.'}}


def protocols(cfg, args):
    template = Path(__file__).resolve().parents[1] / 'examples/h3/protocol'
    source = Path(cfg['protocols'].get('full', cfg['protocols']['screen'])['directory'])
    if not (source / 'h3.json').exists():
        if args.short_starship_request or args.refresh_recipes:
            raise LoopError('These measurement options require the H3 protocol adapter')
        plan = cfg.setdefault('measurement_plan', {'warmups': 2, 'measurements': 2})
        for key in ('warmups', 'measurements'):
            if getattr(args, key) is not None:
                plan[key] = getattr(args, key)
        if plan['warmups'] < 0 or plan['measurements'] < 1:
            raise LoopError('Invalid measurement plan')
        if (source / 'unprepared.py').exists():
            target = args.output.with_suffix('').with_name(args.output.stem + '-protocol')
            copy_inputs(source, target, list(manifest(source)))
            # Reuse the existing numerical/provenance helpers without duplicating them.
            for name in ('local_validation.py', 'support.py'):
                shutil.copy2(template / name, target / name)
            cfg['protocols']['screen'].update(directory=str(target.resolve()),
                instructions=str((target / 'instructions.txt').resolve()))
        return
    target = args.output.with_suffix('').with_name(args.output.stem + '-protocol')
    copy_inputs(source, target, list(manifest(source)))
    for name in ('component.py', 'local_validation.py', 'full.py', 'recipe.py', 'server.py', 'bridge.py', 'support.py', 'sitecustomize.py', 'instructions.txt'):
        shutil.copy2(template / name, target / name)
    spec = read(target / 'h3.json')
    warmups = args.warmups if args.warmups is not None else spec.get('warmups', 2)
    measurements = args.measurements if args.measurements is not None else spec.get('measurements', 3)
    if warmups < 0 or measurements < 1:
        raise LoopError('Warmups must be nonnegative and measurements positive')
    spec.update(warmups=warmups, measurements=measurements)
    if spec.get('captures'):
        spec['local_validation'] = local_contract(spec, read(args.numerical_rule) if getattr(args, 'numerical_rule', None) else None)
        cfg['protocols']['screen'].update(quality_key='numerical_ok', local_validation=spec['local_validation'])
    if args.refresh_recipes:
        cfg['recipe_refresh'] = {'previous_hashes': spec['recipe_hashes'], 'reason': 'Explicitly requested at new-task preparation'}
        spec['recipe_hashes'] = {p: sha(p) for p in spec['recipe_hashes']}
    write(target / 'h3.json', spec)
    old_short = cfg['protocols'].get('short')
    for name, stage in cfg['protocols'].items():
        if name not in ('screen', 'full'):
            continue
        stage.update(directory=str(target.resolve()), instructions=str((target / 'instructions.txt').resolve()))
    if bool(args.short_starship_request) != bool(args.short_dinner_request):
        raise LoopError('Supply both short requests')
    if args.short_starship_request or old_short:
        short = target.with_name(target.name + '-short')
        short_source = target if args.short_starship_request else Path(old_short['directory'])
        copy_inputs(short_source, short, list(manifest(short_source)))
        short_spec = copy.deepcopy(spec) if args.short_starship_request else read(short / 'h3.json')
        short_spec.update(warmups=warmups, measurements=measurements, recipe_hashes=spec['recipe_hashes'])
        for filename in ('component.py', 'local_validation.py', 'full.py', 'recipe.py', 'server.py', 'bridge.py', 'support.py', 'sitecustomize.py', 'instructions.txt'):
            shutil.copy2(template / filename, short / filename)
        for name, request, task in (('starship', args.short_starship_request, 't2va'), ('dinner', args.short_dinner_request, 'fl2va')):
            if request is None:
                continue
            payload = read(request)
            import json
            extra = payload['extra_params']
            extra = json.loads(extra) if isinstance(extra, str) else extra
            if extra['task'] != task:
                raise LoopError('Short request task mismatch')
            shutil.copy2(request, short / short_spec['cases'][name]['request'])
        short_spec['validation_scope'] = 'short_functional'
        write(short / 'h3.json', short_spec)
        stage = copy.deepcopy(cfg['protocols']['full'])
        stage.update(directory=str(short.resolve()), instructions=str((short / 'instructions.txt').resolve()),
                     scope='h3_short_starship_dinner', comparisons=[])
        cfg['protocols']['short'] = stage
    cfg['measurement_plan'] = {'warmups': warmups, 'measurements': measurements,
                               'applies_to': 'performance phases of screen, short, full, and fresh unprofiled baseline'}


def split_short_tasks(cfg):
    """Reuse full.py with one frozen request per native campaign; no outer metric gates."""
    if 'short' not in cfg['protocols']:
        return
    original = cfg['protocols']['short']
    source = Path(original['directory'])
    spec = read(source/'h3.json')
    for case, task in (('starship','t2va'),('dinner','fl2va')):
        if case not in spec['cases']:
            raise LoopError('Short ablation requires both H3 tasks')
        directory = source.with_name(source.name+'-'+task)
        copy_inputs(source,directory,['.'])
        one = copy.deepcopy(spec)
        one['cases'] = {case:spec['cases'][case]}
        one['validation_scope'] = 'short_functional'
        write(directory/'h3.json',one)
        stage = copy.deepcopy(original)
        stage.update(directory=str(directory),instructions=str(directory/'instructions.txt'),
                     comparisons=[],task=task,scope='h3_short_'+task)
        cfg['protocols']['short_'+task] = stage


def prepare(args):
    cfg = copy.deepcopy(read(args.base))
    filled(cfg)
    if args.output.exists():
        raise LoopError('Use a new configuration path; do not overwrite a reviewed task')
    cfg['task'] = args.task
    if args.goal:
        cfg['goal'] = args.goal
    cfg.setdefault('model',{}).update(transport='local_ssh')
    for key in ('name', 'effort'):
        value = getattr(args, 'model' if key == 'name' else key)
        if value:
            cfg['model'][key] = value
    base_protocol = Path(cfg['protocols'].get('full', cfg['protocols']['screen'])['directory'])
    project = args.project
    if project is None and (base_protocol / 'h3.json').exists():
        project = Path(read(base_protocol / 'h3.json')['performance_recipe']).parents[2]
    project = (project or Path(cfg['project']['path'])).resolve()
    if (base_protocol / 'h3.json').exists():
        cfg['planning_scope'] = 'whole_project_minimax_h3'
        cfg['knowledge_query'] = 'MiniMax H3 inference operators attention communication memory scheduling'
        serve = read(base_protocol / 'h3.json').get('serve_arguments', [])
        if 'serve' in serve:
            cfg['model_assets'] = {'root': serve[serve.index('serve') + 1]}
        if not args.goal:
            cfg['goal'] = 'Explore MiniMax-H3 optimizations across the entire repository, including all operators, attention, communication, loading and scheduling, using profiling and history. Existing MLP test coverage is not an edit limit. Use only real-weight local module tests during exploration. The local Codex driver prepares missing module tests before measurement. Qualify candidates by the frozen gain criterion and module numerical validation. Reserve short E2E for later ablation.'
    inputs = project_inputs(project)
    # Preserve installed package extensions for an unchanged native baseline.
    # Native source edits still require separate rebuild/load evidence.
    inputs = sorted(set(inputs) | {str(p.relative_to(project)) for p in (project / 'vllm_omni_kunlun').rglob('*.so')})
    cfg['project'].update(path=str(project), include=inputs, editable=args.editable or ['.'])
    if args.rounds is not None:
        cfg['budget']['max_rounds'] = args.rounds
    if args.seconds is not None:
        cfg['budget']['max_seconds'] = args.seconds
    cfg.setdefault('history_files', []).extend(str(p.resolve()) for p in args.history)
    if getattr(args, 'knowledge_sources', None):
        cfg['knowledge_sources'] = read(args.knowledge_sources)
        filled(cfg['knowledge_sources'])
    if cfg.get('local_recovery'):
        roots = cfg['local_recovery'].setdefault('evidence_roots', [])
        for source in cfg.get('knowledge_sources', []):
            if source['kind'] == 'directory' and source['path'] not in roots:
                roots.append(source['path'])
    if (base_protocol / 'h3.json').exists() and not args.short_starship_request and not args.short_dinner_request and 'short' not in cfg['protocols']:
        short_root = Path('<REMOTE_H3_ROOT>/h3_small_e2e_requests_20261118')
        if (short_root / 'starship-small.json').is_file() and (short_root / 'dining-small.json').is_file():
            args.short_starship_request = short_root / 'starship-small.json'
            args.short_dinner_request = short_root / 'dining-small.json'
    protocols(cfg, args)
    if getattr(args,'knowledge',None):
        cfg.setdefault('knowledge',{})['enabled'] = args.knowledge == 'on'
    if getattr(args,'devices',None):
        devices = [int(x) for x in args.devices.split(',')]
        if len(set(devices)) != len(devices) or any(x < 0 for x in devices):
            raise LoopError('Invalid physical device list')
        for protocol in cfg['protocols'].values():
            resource = protocol.setdefault('resources',{})
            if resource.get('count',0) > len(devices):
                raise LoopError('Device allowance is smaller than protocol requirement')
            resource.update(allow=devices,exclude=[])
    if getattr(args,'after_explore',None):
        cfg['workflow']={'after_explore':args.after_explore}
        if args.after_explore == 'ablate':
            cfg['workflow'].update(ablation_max_seconds=args.ablation_seconds,ablation_stages=['short_t2va','short_fl2va'])
    if cfg.get('workflow',{}).get('after_explore') == 'ablate' or args.task == 'ablate':
        split_short_tasks(cfg)
    mode = args.profile or ('skip' if args.task == 'ablate' else 'reuse')
    old = cfg.get('profile', {})
    profile = {'mode': mode, 'bottleneck': 'Use the supplied evidence and measured feedback to select the next hypothesis'}
    if mode in ('reuse', 'adaptive'):
        path = args.evidence or (Path(old['evidence']) if old.get('evidence') else None)
        if path is None or not path.is_file():
            raise LoopError('Supply --evidence with an existing profiling summary')
        profile['evidence'] = str(path.resolve())
    if mode in ('collect', 'adaptive'):
        if not (base_protocol / 'h3.json').exists():
            if not old.get('argv') or not old.get('directory') or not old.get('timeout_seconds'):
                raise LoopError('Generic collection needs profile.directory, argv and timeout_seconds for a workload-specific collector')
            for key in ('directory', 'argv', 'resources', 'timeout_seconds', 'summary_file'):
                if key in old:
                    profile[key] = copy.deepcopy(old[key])
            profile['max_refreshes'] = args.max_refreshes if mode == 'adaptive' else 0
        else:
            bundle = args.output.with_suffix('').with_name(args.output.stem + '-profiling')
            if bundle.exists():
                raise LoopError('Profiling bundle already exists')
            template = Path(__file__).resolve().parents[1] / 'examples/h3/protocol'
            for name in LEGACY:
                if not (args.collector_source / name).is_file():
                    raise LoopError('Missing legacy profiler input: ' + name)
            (bundle / 'legacy').mkdir(parents=True)
            for name in LEGACY:
                shutil.copy2(args.collector_source / name, bundle / 'legacy' / name)
            for name in ('collect_profile.py', 'support.py', 'sitecustomize.py'):
                shutil.copy2(template / name, bundle / name)
            resources = copy.deepcopy(cfg['protocols'].get('full', cfg['protocols']['screen'])['resources'])
            resources.update(count=2, exclude=list(range(2, 8)), require_host_idle=True)
            profile.update(directory=str(bundle.resolve()),
                           argv=['/opt/venvs/python312_torch29_cuda/bin/python', '{profile}/collect_profile.py',
                                 '--project', '{source}', '--protocol', '{protocol}', '--output', '{output}'],
                           resources=resources, timeout_seconds=args.profile_timeout_seconds,
                           max_refreshes=args.max_refreshes if mode == 'adaptive' else 0)
    cfg['profile'] = profile
    if (base_protocol / 'h3.json').exists():
        cfg['screen_routes'] = [{'paths': p['local_validation']['paths'], 'protocol': name}
                                for name, p in cfg['protocols'].items() if p.get('local_validation', {}).get('paths')]
        cfg.update(enable_full=False, exploration={'validation': 'local_only', 'candidate_basis': 'local_gain_and_module_numerical_pass',
                                                   'weights': 'real_model_only', 'proposal_baseline': 'original_frozen_source',
                                                   'missing_local_test': 'prepare_on_local_codex'})
    if args.local_protocols:
        for entry in read(args.local_protocols):
            name = entry['name']
            if name in cfg['protocols']:
                raise LoopError('Local protocol names must be new')
            cfg['protocols'][name] = entry['protocol']
            cfg.setdefault('screen_routes', []).append({'paths': entry['paths'], 'protocol': name})
    if args.local_min_gain_pct is not None:
        import math
        if not math.isfinite(args.local_min_gain_pct) or args.local_min_gain_pct < 0:
            raise LoopError('Local gain threshold must be a nonnegative finite percentage')
        for route in cfg.get('screen_routes') or [{'protocol': 'screen'}]:
            cfg['protocols'][route['protocol']]['min_improvement_pct'] = args.local_min_gain_pct
    if args.task == 'ablate':
        if not args.ablation_plan:
            raise LoopError('Ablation requires an explicit source-variant matrix')
        cfg['ablation'] = normalize(read(args.ablation_plan))
        # The user's workflow: local candidate discovery, then short-request E2E ablation.
        selected = cfg['ablation'].get('stages', [cfg['ablation'].get('stage', 'short')])
        if selected != ['short']:
            raise LoopError('This H3 workflow uses short-request E2E only for ablation; remove screen/full stages')
        cfg['ablation'].update(stages=['short'], selection_stage='short')
        cfg['ablation'].pop('stage', None)
        if 'short' not in cfg['protocols']:
            raise LoopError('Ablation needs an explicit short-request protocol')
        if args.rounds is None:
            cfg['budget']['max_rounds'] = len(cfg['ablation']['comparisons'])
    else:
        cfg.pop('ablation', None)
    validate(cfg)
    write(args.output, cfg)
    return {'config': str(args.output.resolve()), 'task': args.task, 'profiling': mode,
            'model': cfg['model'], 'note': 'Prepared only; no models or devices were started'}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--base', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--knowledge',choices=('on','off'))
    p.add_argument('--devices',help='Physical IDs, e.g. 0,1 or 2,3')
    p.add_argument('--after-explore',choices=('stop','ablate'))
    p.add_argument('--ablation-seconds',type=int,default=0)
    p.add_argument('--task', choices=('explore', 'ablate'), default='explore')
    p.add_argument('--profile', choices=('reuse', 'collect', 'adaptive', 'skip'))
    p.add_argument('--evidence', type=Path)
    p.add_argument('--ablation-plan', type=Path)
    p.add_argument('--project', type=Path)
    p.add_argument('--goal')
    p.add_argument('--editable', action='append', help='Optional narrower scope; default is the whole project')
    p.add_argument('--warmups', type=int)
    p.add_argument('--measurements', type=int)
    p.add_argument('--local-min-gain-pct', type=float, help='Frozen local candidate gain threshold; otherwise reuse the protocol value')
    p.add_argument('--numerical-rule', type=Path, help='Reviewed module-specific numerical rule JSON; absence means exact output or pending review')
    p.add_argument('--local-protocols', type=Path, help='Additional frozen local adapters: list of name, paths, protocol objects')
    p.add_argument('--short-starship-request', type=Path)
    p.add_argument('--short-dinner-request', type=Path)
    p.add_argument('--refresh-recipes', action='store_true')
    p.add_argument('--model')
    p.add_argument('--effort')
    p.add_argument('--rounds', type=int)
    p.add_argument('--seconds', type=int)
    p.add_argument('--history', type=Path, action='append', default=[])
    p.add_argument('--knowledge-sources', type=Path, help='Explicit document URLs and experiment/reference directories; no inferred private roots')
    p.add_argument('--collector-source', type=Path, default=Path('<REMOTE_H3_ROOT>/reprofile033_official_retry_20261123'))
    p.add_argument('--profile-timeout-seconds', type=int, default=43200)
    p.add_argument('--max-refreshes', type=int, default=1)
    args = p.parse_args()
    import json
    print(json.dumps(prepare(args), ensure_ascii=False))


if __name__ == '__main__':
    main()
