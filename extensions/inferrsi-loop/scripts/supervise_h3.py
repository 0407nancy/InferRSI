"""Detached local supervisor; reuse run_h3 and bounded Codex recovery."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from inferrsi_loop.configuration import launch
from inferrsi_loop import model_selection
from inferrsi_loop.common import read, write, LoopError


def deployment(args):
    return launch(args[args.index('--launch-config')+1]) if '--launch-config' in args else {}

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--task-file', required=True, help='Persisted user requirements and evidence locations')
    p.add_argument('--recoveries', type=int, default=3)
    p.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('args', nargs=argparse.REMAINDER)
    a = p.parse_args()
    args = a.args[1:] if a.args[:1] == ['--'] else a.args
    if '--run-id' not in args or a.recoveries < 0:
        p.error('Require --run-id and nonnegative recovery budget')
    run_id = args[args.index('--run-id') + 1]
    if not run_id.replace('-', '').replace('_', '').isalnum():
        p.error('Invalid run ID')
    task = Path(a.task_file).resolve()
    if not task.is_file():
        p.error('Task file must exist')
    folder = Path(deployment(args).get('local_runs', str(ROOT.parent / 'inferrsi-loop-runs'))) / run_id
    folder.mkdir(parents=True, exist_ok=True)
    if not a.worker:
        if not (folder/'parent-session.json').exists():
            write(folder/'parent-session.json', model_selection.session_context())
        command = [sys.executable, str(Path(__file__).resolve()), '--worker', '--task-file', str(task),
                   '--recoveries', str(a.recoveries), '--', *args]
        with (folder / 'supervisor.log').open('ab') as log:
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    start_new_session=True, close_fds=True)
        print(json.dumps({'pid': proc.pid, 'directory': str(folder)}))
        return
    with (folder / '.supervisor.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (folder / 'task.txt').write_text(task.read_text())
        (folder / 'supervisor-config.json').write_text(json.dumps({'args': args, 'recoveries': a.recoveries}))
        from preflight_access import check
        access = check(args[args.index('--launch-config')+1]) if '--launch-config' in args else check()
        (folder / 'access-preflight.json').write_text(json.dumps(access))
        if not access['ready']:
            (folder / 'supervisor-status.json').write_text(json.dumps({
                'status': 'needs_attention', 'reason': 'access_preflight_failed'}))
            return
        awake = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())]) if sys.platform == 'darwin' else None
        try:
            supervise(folder, args, a.recoveries)
        finally:
            if awake:
                awake.terminate()

def supervise(folder, args, budget):
    def option(name, default):
        return args[args.index(name)+1] if name in args else default
    selected = deployment(args)
    binary = option('--codex', selected.get('codex', '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'))
    report_only = False
    for attempt in range(budget + 1):
        command = [sys.executable, str(ROOT / 'scripts/run_h3.py'), *args]
        if report_only and '--report-only' not in args:
            command.append('--report-only')
        with (folder / f'attempt-{attempt}.log').open('w') as log:
            rc = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log).returncode
        state = {'attempt': attempt, 'returncode': rc, 'status': 'complete' if rc == 0 else 'recovering'}
        (folder / 'supervisor-status.json').write_text(json.dumps(state))
        if rc == 0:
            return
        # Sync failures must not trigger another experiment.
        report_only = report_only or rc == 3
        if attempt == budget:
            state['status'] = 'needs_attention'
            (folder / 'supervisor-status.json').write_text(json.dumps(state))
            return
        schema = folder / 'recovery-schema.json'
        answer = folder / f'recovery-{attempt}.json'
        schema.write_text(json.dumps({'type':'object','properties':{
            'disposition':{'type':'string','enum':['retry','report_only','needs_attention']},
            'reason':{'type':'string'}},'required':['disposition','reason'],'additionalProperties':False}))
        prompt = f'''Read README.md and {folder}/task.txt, supervisor-config.json, remote-status.json and attempt-{attempt}.log in {folder}. You are recovering an existing authorized run, with no chat context. Diagnose and fix only recoverable infrastructure/adapter issues. If sandbox blocks authorized network or credential-store access, request targeted escalation through automatic approval review; never disable sandbox or change auth configuration. Inspect remote state before actions; never duplicate an in-flight experiment. Do not start the driver: the supervisor retries it after you exit. Preserve frozen inputs, budgets, numerical gates, evidence and the configured device allowlist. Never kill unrelated jobs, change credentials, disable host verification, or rewrite frozen runs. If frozen artifacts need changes or authorization is missing, leave an explanation in {folder}/recovery-{attempt}.txt and exit. For publication failures only repair/report retained results; do not launch experiments. When remote-status.json knowledge_enabled is false, do not consult other runs or historical optimization documents for recovery; use this run evidence and task facts only. Read applicable AGENTS.md and lark-doc skill before Feishu operations. Save diagnosis and changes in recovery-{attempt}.txt. Return the structured disposition: retry only when a concrete fix or verified transient recovery permits it; report_only for pending publication; needs_attention for unresolved permission/frozen-runtime issues. Protocol revisions and evidence acquisition are owned by the loop lifecycle; do not mutate its state. Never repeat an unchanged deterministic error. Requirements:\n{(folder / 'task.txt').read_text()}'''
        try:
            selection_path = folder/'model-selection.json'
            selection = read(selection_path) if selection_path.exists() else model_selection.resolve(
                {'model':option('--model',selected.get('model')),'effort':option('--effort',selected.get('effort'))},
                session=read(folder/'parent-session.json') if (folder/'parent-session.json').exists() else None)
        except LoopError as exc:
            state.update(status='needs_attention',reason=str(exc))
            write(folder/'supervisor-status.json',state)
            return
        with (folder / f'recovery-{attempt}.log').open('w') as log:
            try:
                subprocess.run([binary, 'exec', '--model', selection['model'], '-c', 'model_reasoning_effort='+json.dumps(selection['effort']),
                                '--approve-for-me', '--skip-git-repo-check', '--cd', str(ROOT),
                                '--output-schema', str(schema), '--output-last-message', str(answer), '-'],
                               input=prompt, text=True, stdout=log, stderr=log, timeout=900)
            except subprocess.TimeoutExpired:
                log.write('\nRecovery timed out; evidence retained.\n')
        try:
            disposition = json.loads(answer.read_text())
        except (OSError, ValueError):
            disposition = {'disposition':'needs_attention','reason':'Recovery returned no valid disposition'}
        if disposition.get('disposition') not in ('retry','report_only'):
            state.update(status='needs_attention', recovery=disposition)
            (folder / 'supervisor-status.json').write_text(json.dumps(state))
            return
        report_only = report_only or disposition['disposition'] == 'report_only'
        time.sleep(10)

if __name__ == '__main__':
    main()
