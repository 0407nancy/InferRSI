"""Reuse the established H3 torch/xprofiler scripts in an isolated job folder.

Both profilers are collected serially with identical frozen inputs. Their
latencies are diagnostic only; a separate profiler-free recipe measures baseline.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import runpy
import shutil
import signal
import subprocess
import sys
import tempfile

from support import environment, load, save, sha

LEGACY = ('run.py', 'xprof_run.py', 'server.py', 'xprof_request.py',
          'analyze_torch_trace.py', 'analyze_stages.py', 'analyze_xprof.py', 'audit_device_timeline.py')
_owned = []


def cleanup():
    for process in _owned:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def terminate(*_):
    cleanup()
    raise SystemExit(143)


def script(path, arguments):
    """Track the legacy scripts' detached server/collector process groups."""
    original, argv = subprocess.Popen, sys.argv
    def tracked(*args, **kwargs):
        process = original(*args, **kwargs)
        if kwargs.get('start_new_session'):
            _owned.append(process)
        return process
    subprocess.Popen = tracked
    sys.argv = [str(path), *map(str, arguments)]
    try:
        runpy.run_path(str(path), run_name='__main__')
    finally:
        cleanup()
        _owned.clear()
        subprocess.Popen, sys.argv = original, argv


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError('Legacy profiler interface changed; inspect before adapting: ' + old[:80])
    return text.replace(old, new)


def prepare_legacy(bundle, target):
    target.mkdir()
    for name in LEGACY:
        text = (bundle / 'legacy' / name).read_text()
        if name in ('run.py', 'xprof_run.py'):
            text = replace_once(text, "old=Path('<REMOTE_H3_ROOT>/perf_20261106')",
                                "old=Path(os.environ['INFERRSI_PROFILE_INPUTS'])")
            text = replace_once(text, "image=Path('<REMOTE_H3_ROOT>/first.png')",
                                "image=Path(os.environ['INFERRSI_PROFILE_IMAGE'])")
            # Legacy code clears PYTHONPATH; restore only the verified frozen source.
            text = replace_once(text, "            proc=subprocess.Popen(cmd,cwd='/tmp',env=env",
                                "            env.update(PYTHONPATH=os.environ['PYTHONPATH'], INFERRSI_H3_PROJECT=os.environ['INFERRSI_H3_PROJECT'])\n            proc=subprocess.Popen(cmd,cwd='/tmp',env=env")
        if name == 'xprof_run.py':
            text = replace_once(text, "env_socket=Path('/tmp/<PROFILE_SOCKET_PREFIX>'+name+'-'+kind)",
                                "env_socket=Path(os.environ['INFERRSI_PROFILE_SOCKET'])/name")
        compile(text, str(target / name), 'exec')
        (target / name).write_text(text)


def top(rows, count=5):
    return [{k: (v[:200] if k == 'name' and isinstance(v, str) else v) for k, v in row.items()}
            for row in rows[:count]]


def summarize(capture, case):
    name = {'starship': 't2va', 'dinner': 'fl2va'}[case] + '_official'
    torch_rows = load(capture / (name + '_torch_analysis.json'))
    stages = load(capture / (name + '_stage_analysis.json'))
    timeline = load(capture / (name + '_xprof/timeline_audit.json'))
    xprof = load(capture / (name + '_xprof/analysis_summary.json'))
    if len(torch_rows) != 2 or len(stages['workers']) != 2 or len(timeline['devices']) < 2 or not xprof['events']:
        raise RuntimeError('Missing rank/device profiling evidence')
    workers = []
    for row in torch_rows:
        stage = next(item for item in stages['workers'] if item['trace'] == row['trace'])
        workers.append({**{k: row[k] for k in ('trace', 'kernel_span_s', 'kernel_union_s', 'kernel_coverage_pct',
                                               'gaps_over_5ms_count', 'gaps_over_5ms_s', 'largest_gaps_s')},
                        'hot_kernels': top(row['categories']['kernel']['top']),
                        'hot_cpu_ops': top(row['categories']['cpu_op']['top']),
                        'stage_coverage': stage['coverage'],
                        'stages': [{**s, 'top_kernels': top(s['top_kernels'], 3)} for s in stage['stages']
                                   if s['name'] in ('h3_stage::encode_prompt', 'h3_stage::diffuse', 'h3_stage::decode')]})
    for row in timeline['devices'].values():
        row['largest_gaps_seconds_from_first'] = row['largest_gaps_seconds_from_first'][:3]
        for gap in row['largest_gaps_seconds_from_first']:
            for key in ('previous_kernel', 'next_kernel'):
                gap[key] = gap[key][:200]
    return {'torch_workers': workers, 'stage_method': stages['method'], 'xprofiler_timeline': timeline,
            'xprofiler_hot_events': top(xprof['top']),
            'note': 'Separate captures; do not add profiler times or infer CPU causality from a gap. Full names remain in raw analysis files.'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--preflight', action='store_true')
    args = parser.parse_args()
    protocol, project = args.protocol.resolve(), args.project.resolve()
    bundle, output = Path(__file__).resolve().parent, args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    spec = load(protocol / 'h3.json')
    for path, expected in spec['recipe_hashes'].items():
        if sha(path) != expected:
            raise RuntimeError('Referenced performance recipe changed')
    if not (project / 'vllm_omni_kunlun/models/minimax_h3/projection_pipeline.py').is_file():
        raise RuntimeError('Missing frozen H3 source')
    collector = Path('/opt/venvs/python312_torch29_cuda/xprofiler/bin/xprofiler')
    if not collector.is_file():
        raise RuntimeError('xprofiler executable missing')
    importlib.metadata.version('ijson')
    capture = output / 'capture'
    prepare_legacy(bundle, capture)
    inputs = output / 'inputs'
    for case, config in spec['cases'].items():
        legacy_name = 'starship_50_split128' if case == 'starship' else 'dinner_baseline'
        folder = inputs / 'recovery' / legacy_name
        folder.mkdir(parents=True)
        shutil.copy2(protocol / config['request'], folder / 'request.json')
        save(folder / 'environment.json', {'command': [sys.executable, '-m', 'vllm_omni.entrypoints.cli.main', *spec['serve_arguments']],
                                          'first_frame_sha256': config.get('first_frame_sha256')})
    save(output / 'preflight.json', {'status': 'prepared', 'legacy_hashes': {n: sha(bundle / 'legacy' / n) for n in LEGACY},
                                    'project': str(project), 'protocol': str(protocol), 'profilers': ['torch', 'xprofiler'],
                                    'capture_order': 'serial', 'performance_baseline': {'warmups': spec.get('warmups', 2), 'measurements': spec.get('measurements', 3)}})
    if args.preflight:
        return
    if os.environ.get('XPU_VISIBLE_DEVICES') != '0,1' or os.environ.get('CUDA_VISIBLE_DEVICES') != '0,1':
        raise RuntimeError('This H3 collector requires reserved physical devices 0,1')
    if subprocess.run(['pgrep', '-x', 'xprofiler'], stdout=subprocess.DEVNULL).returncode == 0:
        raise RuntimeError('Another xprofiler is active; leave it untouched')
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    env = environment(project, spec['environment'])
    os.environ.clear()
    os.environ.update(env)
    # The legacy bootstrap can have imported the installed plugin before main.
    runpy.run_path(str(protocol / 'sitecustomize.py'))
    with tempfile.TemporaryDirectory(prefix='h3prof-') as sockets:
        os.environ.update(H3_PROFILE_DEVICES='0,1', INFERRSI_PROFILE_INPUTS=str(inputs),
                          INFERRSI_PROFILE_IMAGE=str(protocol / 'first.png'), INFERRSI_PROFILE_SOCKET=sockets)
        result = {'scope': 'fresh_h3_dual_profiler', 'source_hash': os.environ.get('INFERRSI_PROFILE_SOURCE_HASH'),
                  'cases': {}, 'unprofiled_baseline': {}, 'versions': {}}
        for package in ('torch', 'vllm', 'vllm-omni', 'torch-xmlir'):
            try:
                result['versions'][package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                result['versions'][package] = 'unavailable'
        for case, task in (('starship', 't2va'), ('dinner', 'fl2va')):
            script(capture / 'run.py', [task, 'official'])
            script(capture / 'analyze_torch_trace.py', [task + '_official'])
            script(capture / 'analyze_stages.py', [task + '_official'])
            script(capture / 'xprof_run.py', [task, 'official'])
            trace = capture / (task + '_official_xprof/xprofiler.json')
            script(capture / 'analyze_xprof.py', [trace])
            script(capture / 'audit_device_timeline.py', [trace])
            result['cases'][case] = summarize(capture, case)
            destination = output / 'unprofiled' / case
            arguments = ['--recipe', spec['performance_recipe'], '--project', project, '--',
                         '--model', '/models/MiniMax-H3', '--request', protocol / spec['cases'][case]['request'],
                         '--xpu-ids', '0,1', '--result-dir', destination, '--warmups', str(spec.get('warmups', 2)), '--measures', str(spec.get('measurements', 3))]
            if case == 'dinner':
                arguments += ['--first-frame', protocol / 'first.png']
            script(protocol / 'recipe.py', arguments)
            metrics = load(destination / 'metrics.json')
            if metrics['success_count'] != spec.get('measurements', 3) or metrics['warmups'] != spec.get('warmups', 2):
                raise RuntimeError('Incomplete unprofiled baseline')
            result['unprofiled_baseline'][case] = metrics
        result['artifact_hashes'] = {str(p.relative_to(output)): sha(p) for p in output.rglob('*')
                                    if p.is_file() and (p.name.endswith(('.json', '.json.gz')))
                                    and not p.is_relative_to(output / 'execution') and p.name not in ('job.json', 'result.json', 'summary.json')}
        save(output / 'summary.json', result)


if __name__ == '__main__':
    main()
