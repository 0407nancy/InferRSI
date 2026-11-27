"""Bounded evidence acquisition and append-only local protocol revisions."""
import base64
import copy
import json
import os
from pathlib import Path
import sys
import time

from .common import LoopError, copy_inputs, digest, manifest, read, run_command, safe_child, sha, write
from .runner import resources


def enabled(config):
    return bool(config.get('local_recovery', {}).get('enabled'))


def cursor(folder):
    p = Path(folder) / 'protocol-state.json'
    return read(p) if p.exists() else {'version': 1, 'status': 'generation', 'repairs': []}


def version_dir(folder):
    return Path(folder) / 'protocol-versions' / f"v{cursor(folder)['version']:02d}"


def logs(root):
    result = {}
    for p in sorted(Path(root).rglob('*.log')):
        if p.name in ('stderr.log', 'stdout.log'):
            result[str(p.relative_to(root))] = p.read_text(errors='replace')[-10000:]
    return dict(list(result.items())[-12:])


def revise(config, folder, reason, evidence):
    state = cursor(folder)
    signature = digest({'reason': reason, 'logs': evidence})
    entry = {'version': state['version'], 'reason': reason, 'evidence': evidence, 'fingerprint': signature}
    state['repairs'].append(entry)
    limit = config.get('local_recovery', {}).get('max_protocol_versions', 4)
    duplicate = sum(x['fingerprint'] == signature for x in state['repairs']) > 1
    state.update(status='exhausted' if state['version'] >= limit or duplicate else 'generation',
                 version=state['version'] + (0 if state['version'] >= limit or duplicate else 1))
    write(Path(folder) / 'protocol-state.json', state)
    return state['status'] != 'exhausted'


def preflight(config, run, folder):
    """Baseline-only quality execution after relocation, before activation/freeze."""
    state = cursor(folder)
    if state['status'] != 'preflight':
        return
    version = version_dir(folder)
    draft = read(version / 'draft.json')
    stage = draft['protocol']
    receipt = version / 'preflight.json'
    if receipt.exists():
        result = read(receipt)
    else:
        output = version / 'preflight'
        output.mkdir(parents=True, exist_ok=True)
        protocol = output / 'relocated-protocol'
        if not protocol.exists():
            copy_inputs(stage['directory'], protocol, list(manifest(stage['directory'])))
        expected = manifest(protocol)
        result_file = output / 'result.json'
        baseline = Path(run) / 'frozen/baseline'
        values = {'python': sys.executable, 'protocol': str(protocol), 'candidate': str(baseline),
                  'baseline': str(baseline), 'result': str(result_file)}
        argv = [part.format(**values) for part in stage['quality_command']]
        deadline = read(Path(run) / 'state.json')['deadline']
        timeout = min(config.get('local_recovery', {}).get('preflight_timeout_seconds', 240), deadline-time.time())
        if timeout <= 0:
            raise LoopError('No preflight budget remains')
        if (output / 'execution/process.json').exists() and not result_file.exists():
            # Do not launch a second copy of a possibly interrupted process.
            raise LoopError('Unsettled preflight; inspect retained process before resuming')
        with resources(stage.get('resources', {}), output) as env:
            env.update(PYTHONDONTWRITEBYTECODE='1')
            execution = run_command(argv, protocol, output / 'execution', timeout, env=env)
        try:
            value = read(result_file) if result_file.exists() else {}
        except (OSError, ValueError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        proof = value.get('local_validation', {})
        passed = (execution['status'] == 'completed' and value.get('numerical_ok') is True
                  and proof.get('weights_verified') is True and proof.get('numerical_status') == 'pass'
                  and proof.get('contract_digest') == digest(stage['local_validation']))
        # Result/logs live outside the copied protocol; the protocol must stay immutable.
        passed = passed and manifest(protocol) == expected
        result = {'passed': passed, 'execution': execution, 'result': value,
                  'protocol_digest': digest(stage), 'logs': logs(output)}
        write(receipt, result)
    if result['passed']:
        if manifest(stage['directory']) != draft['files']:
            raise LoopError('Draft protocol changed during preflight')
        write(Path(folder) / 'local-test-receipt.json', draft)
        state['status'] = 'ready'
        write(Path(folder) / 'protocol-state.json', state)
    else:
        revise(config, folder, 'baseline_preflight_failed', result['logs'] or result)


def supplement(config, run, folder, payload):
    """Read allowlisted files/headers only; no arbitrary shell or weight transfer."""
    request = read(Path(folder) / 'local-test-request.json')
    if payload['request_digest'] != digest(request):
        raise LoopError('Stale evidence request')
    requests = payload.get('missing_evidence', [])
    root = Path(folder) / 'supplements'
    previous = list(root.glob('*/receipt.json'))
    fingerprint = digest(requests)
    for p in previous:
        value = read(p)
        if value['fingerprint'] == fingerprint:
            return dict(value, repeated=True)
    if len(previous) >= config.get('local_recovery', {}).get('max_evidence_rounds', 6):
        return {'status': 'exhausted'}
    target = root / f'{len(previous)+1:02d}'
    target.mkdir(parents=True)
    roots = [Path(x).resolve() for x in config.get('local_recovery', {}).get('evidence_roots', [])]
    rows = []
    for i, item in enumerate(requests[:12]):
        try:
            path = Path(item['path']).resolve()
            if not any(path == r or path.is_relative_to(r) for r in roots):
                raise LoopError('Outside declared evidence roots')
            kind = item['kind']
            if kind == 'list':
                content = json.dumps([{'name': p.name, 'directory': p.is_dir(), 'size': p.stat().st_size}
                                      for p in sorted(path.iterdir())[:200]])
            elif kind == 'safetensors_header':
                import struct
                if path.suffix != '.safetensors':
                    raise LoopError('Expected safetensors file')
                with path.open('rb') as inp:
                    size = struct.unpack('<Q', inp.read(8))[0]
                    if size > 16*1024*1024:
                        raise LoopError('Header too large')
                    header = json.loads(inp.read(size))
                content = json.dumps({k: {'shape': v['shape'], 'dtype': v['dtype']}
                                      for k, v in header.items() if k != '__metadata__'})
            else:
                if kind != 'text' or path.suffix not in ('.py', '.json', '.txt', '.md', '.toml', '.yaml', '.log'):
                    raise LoopError('Expected text source/metadata/log file (.py/.json/.txt/.md/.toml/.yaml/.log)')
                if not path.is_file():
                    raise LoopError('Expected regular text file')
                limit = 2*1024*1024
                if path.stat().st_size > limit:
                    raise LoopError('Text evidence exceeds 2 MiB; request a smaller existing source/metadata file')
                # Bound the actual read too: a live log may grow after stat().
                with path.open('rb') as inp:
                    raw = inp.read(limit + 1)
                if len(raw) > limit:
                    raise LoopError('Text evidence exceeds 2 MiB; request a smaller existing source/metadata file')
                content = raw.decode('utf-8')
            file = f'{i:02d}-{path.name}.txt'
            (target / file).write_text(content)
            rows.append({'path': str(path), 'kind': kind, 'file': file, 'content_sha256': sha(target/file), 'status': 'read'})
        except (OSError, ValueError, KeyError, LoopError) as exc:
            rows.append({'request': item, 'status': 'unavailable', 'reason': str(exc)})
    receipt = {'status': 'collected', 'fingerprint': fingerprint, 'requests': requests, 'files': rows,
               'reason': payload.get('reason'), 'sequence': len(previous)+1}
    write(target / 'receipt.json', receipt)
    return receipt
