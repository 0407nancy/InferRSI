"""Prompt-selected profiling policy; execute only frozen collector commands."""
from pathlib import Path
import time

from . import runner
from .common import LoopError, digest, manifest, read, safe_child, sha, write


def mode(spec):
    return spec.get('mode', 'reuse' if spec.get('evidence') else 'collect')


def validate(spec, task):
    selected = mode(spec)
    if selected not in ('reuse', 'collect', 'adaptive', 'skip'):
        raise LoopError('Unknown profiling mode')
    if selected == 'skip':
        if task != 'ablate':
            raise LoopError('Exploration requires profiling evidence')
        return
    if selected in ('reuse', 'adaptive') and not spec.get('evidence'):
        raise LoopError('Reuse/adaptive requires an existing evidence file')
    if selected in ('collect', 'adaptive'):
        if not spec.get('argv') or spec.get('timeout_seconds', 0) <= 0:
            raise LoopError('Collection requires a frozen command and timeout')
    if selected == 'adaptive' and (type(spec.get('max_refreshes')) is not int or spec['max_refreshes'] < 1):
        raise LoopError('Adaptive profiling requires a positive max_refreshes budget')


def request(config, run, state, reason, context_digest):
    spec = config['profile']
    if mode(spec) != 'adaptive':
        raise LoopError('This run does not authorize additional profiling')
    if not isinstance(reason, str) or not reason.strip():
        raise LoopError('A profiling request must explain the missing evidence')
    directory = Path(run) / 'profiling'
    requests = sorted(directory.glob('request-*.json'))
    # A retried transport message is idempotent, including after collection.
    for path in requests:
        value = read(path)
        if value['context_digest'] == context_digest:
            if value['reason'] != reason:
                raise LoopError('Conflicting profiling request for the same context')
            return value
    if len(requests) >= spec['max_refreshes']:
        raise LoopError('Additional profiling budget exhausted')
    value = {'number': len(requests) + 1, 'reason': reason, 'context_digest': context_digest,
             'round': state['round'], 'source': state['current_source'],
             'source_hash': digest(manifest(state['current_source'])), 'requested_at': time.time()}
    write(directory / f'request-{value["number"]:03d}.json', value)
    return value


def evidence(config, run, deadline):
    spec = config['profile']
    selected = mode(spec)
    run = Path(run)
    requests = sorted((run / 'profiling').glob('request-*.json'))
    generation = len(requests)
    source = run / 'frozen/baseline'
    if selected == 'skip':
        path = run / 'ablation-plan.json'
        write(path, {'purpose': 'Fixed ablation matrix; no profiling was requested', 'plan': config['ablation']})
    elif requests or selected == 'collect':
        if requests:
            latest = read(requests[-1])
            source = Path(latest['source'])
            if digest(manifest(source)) != latest['source_hash']:
                raise LoopError('Source changed after profiling was requested')
        output = run / 'jobs' / f'profile-{generation:03d}'
        protocol = config['protocols'].get('full', config['protocols']['screen'])['directory']
        replacements = {'{profile}': spec.get('directory', ''), '{protocol}': protocol}
        argv = spec['argv'][:]
        for old, new in replacements.items():
            argv = [part.replace(old, new) for part in argv]
        job = {'argv': argv, 'cwd': str(source), 'timeout_seconds': spec['timeout_seconds'],
               'required_artifacts': [spec.get('summary_file', 'summary.json')],
               'input_hashes': spec.get('input_hashes', {})}
        # Keep the job identity stable across resume; never reacquire busy cards
        # merely to read completed evidence.
        if (output / 'result.json').exists():
            result = runner.execute(job, output)
        else:
            if deadline - time.time() < spec['timeout_seconds']:
                raise LoopError('Remaining run budget cannot cover the authorized profiling timeout')
            with runner.resources(spec.get('resources', {}), output) as env:
                env['INFERRSI_PROFILE_SOURCE_HASH'] = digest(manifest(source))
                result = runner.execute(job, output, env=env)
        occupancy = output / 'occupancy.json'
        if occupancy.exists() and (read(occupancy)['external_activity'] or read(occupancy)['probe_failed']):
            raise LoopError('Collected baseline isolation was invalid; profiling artifacts are retained for inspection')
        if result['status'] != 'completed':
            raise LoopError('Profiling evidence incomplete; inspect the retained collection job')
        path = safe_child(output, spec.get('summary_file', 'summary.json'))
        doc = read(path)
        for name, expected in doc.get('artifact_hashes', {}).items():
            if sha(safe_child(output, name)) != expected:
                raise LoopError('Collected profiling evidence changed')
    else:
        path = Path(spec['evidence'])
    return {'bottleneck': spec.get('bottleneck', 'Select a hypothesis from the supplied profiling evidence'),
            'evidence': str(path), 'mode': selected, 'generation': generation,
            'source': str(source), 'reuse_note': 'Existing traces guide hypotheses; fresh paired measurements establish gains',
            'refreshes_remaining': max(0, spec.get('max_refreshes', 0) - generation) if selected == 'adaptive' else 0}


def excerpt(path):
    import json
    text = Path(path).read_text()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            data.pop('artifact_hashes', None)
        text = json.dumps(data, ensure_ascii=False)
    except ValueError:
        pass
    return text if len(text) <= 32000 else text[:32000] + '\n[truncated; inspect retained evidence before assuming missing details]'
