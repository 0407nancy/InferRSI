"""Local Codex → authenticated SSH → existing InferRSI campaigns on 93."""
import argparse
import base64
import gzip
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from inferrsi_loop import adapter, local_tests, planner, knowledge
from inferrsi_loop.recovery import archive_incomplete
from inferrsi_loop.common import LoopError, lock, manifest, read, safe_child, write


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--remote-run", required=True)
    p.add_argument("--remote-config", required=True)
    p.add_argument("--host", default="93")
    p.add_argument("--ssh", default="<SSH_PROGRAM>")
    p.add_argument("--container", default="<CONTAINER>")
    p.add_argument("--remote-python", default="<REMOTE_H3_ROOT>/python")
    p.add_argument('--remote-package', default='<REMOTE_H3_ROOT>/inferrsi-loop')
    p.add_argument('--expected-user', default='<REMOTE_USER>')
    p.add_argument('--lark', default='lark-cli')
    p.add_argument("--codex", default="/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
    p.add_argument("--model", default="gpt-6-astra")
    p.add_argument("--effort", default="medium")
    p.add_argument("--wait-for-devices", action="store_true")
    a = p.parse_args()
    a.root = a.root.resolve()
    a.root.mkdir(parents=True, exist_ok=True)
    settings = {"name": a.model, "effort": a.effort, "codex": a.codex,
                "codex_home": str(Path.home() / ".codex"), "timeout_seconds": 600}
    def remote(action, **fields):
        command = ["docker", "exec", "-i", '-e', 'PYTHONPATH=' + a.remote_package.rstrip('/') + '/src',
                   a.container, a.remote_python, "-m", "inferrsi_loop.ssh_step"]
        compressed = action in ('plan', 'test_plan')
        if compressed:
            # Keep large source bundles compact across the SSH connection.
            code = """import base64,gzip,json,sys
from inferrsi_loop.ssh_step import dispatch
from inferrsi_loop.common import LoopError
try:
    value=dispatch(json.load(sys.stdin)); status=0
except (LoopError,OSError,ValueError,KeyError) as exc:
    value={'status':'error','reason':str(exc)}; status=2
print(base64.b64encode(gzip.compress(json.dumps(value,ensure_ascii=False).encode())).decode())
raise SystemExit(status)
"""
            command = command[:-2] + ['-c', code]
        payload = {"action": action, "run": a.remote_run, "config": a.remote_config, **fields}
        attempts = 3 if action in ('plan', 'test_plan', 'status') else 1
        for attempt in range(attempts):
            proc = subprocess.run([a.ssh, "-T", a.host, shlex.join(command)],
                                  input=json.dumps(payload).encode(), capture_output=True, timeout=172920)
            try:
                data = gzip.decompress(base64.b64decode(proc.stdout.strip(), validate=True)) if compressed else proc.stdout
                result = json.loads(data)
            except (ValueError, OSError, EOFError) as exc:
                if attempt + 1 < attempts:
                    time.sleep(1)
                    continue
                raise LoopError("SSH step did not return a JSON receipt (exit " + str(proc.returncode) + ")") from exc
            if proc.returncode:
                raise LoopError(result.get("reason", "SSH step failed"))
            return result
    with lock(a.root / ".driver.lock"):
        identity = subprocess.run([a.ssh, "-T", a.host, "id -un"], capture_output=True, text=True, timeout=90, check=True)
        if identity.stdout.strip() != a.expected_user:
            raise LoopError("SSH did not return the personal Linux user")
        setup = remote('configuration')
        if not setup['initialized']:
            snapshots = knowledge.fetch_documents(setup['knowledge_sources'], a.lark)
            write(a.root / 'knowledge-documents.json', snapshots)
            remote('initialize', documents=snapshots)
        while True:
            result = remote("advance")
            write(a.root / "remote-status.json", result)
            print(json.dumps({key: result.get(key) for key in ("status", "completed_rounds", "candidate_count", "effective_experiments", "protocol_repairs", "evidence_rounds", "last_stage", "last_verdict", "reason")}), flush=True)
            if result["status"] == "waiting_resources" and a.wait_for_devices:
                time.sleep(30)
                continue
            if result['status'] == 'ready':
                continue
            if result["status"] not in ("awaiting_local_proposal", "awaiting_local_test"):
                return
            checks = adapter.doctor(settings, a.root / "adapter")
            if not checks["ready"]:
                raise LoopError("The local Codex CLI is not ready")
            if result['status'] == 'awaiting_local_test':
                plan = remote('test_plan')
                if (plan['model']['name'], plan['model'].get('effort', 'medium')) != (a.model, a.effort):
                    raise LoopError('Local and frozen remote model selections differ')
                settings['timeout_seconds'] = min(900, max(1, plan['deadline'] - time.time()))
                version = plan['request'].get('protocol_state', {}).get('version', 1)
                supplement = plan['request'].get('supplement_count', 0)
                folder = a.root / 'rounds' / str(plan['request']['round']) / f'local-test-v{version:02d}-e{supplement:02d}'
                archive_incomplete(folder, "prepared.json")
                prepared = local_tests.prepare(settings, plan, folder)
                if prepared.get('ready') is False:
                    if prepared.get('missing_evidence'):
                        evidence = remote('test_evidence', request_digest=prepared['request_digest'],
                                          missing_evidence=prepared['missing_evidence'], reason=prepared['reason'])
                        write(folder / 'evidence-receipt.json', evidence)
                        if evidence.get('status') == 'collected' and not evidence.get('repeated'):
                            continue
                    receipt = remote('test_unavailable', request_digest=prepared['request_digest'], reason=prepared['reason'])
                    write(folder / 'transfer-receipt.json', receipt)
                    continue
                protocol = folder / 'workspace/protocol'
                try:
                    receipt = remote('test_submit', request_digest=prepared['request_digest'], summary=prepared['summary'],
                                     files={name: base64.b64encode((protocol / name).read_bytes()).decode() for name in manifest(protocol)})
                except LoopError as exc:
                    if 'protocol_state' not in plan['request']:
                        raise
                    remote('test_reject', request_digest=prepared['request_digest'], reason=str(exc))
                    continue
                write(folder / 'transfer-receipt.json', receipt)
                continue
            plan = remote("plan")
            if (plan["model"]["name"], plan["model"].get("effort", "medium")) != (a.model, a.effort):
                raise LoopError("Local and frozen remote model selections differ")
            folder = a.root / "rounds" / str(plan["round"])
            folder.mkdir(parents=True, exist_ok=True)
            generation = plan["context"]["profile"].get("generation", 0)
            folder = folder if generation == 0 else folder / ("profile-" + str(generation))
            folder.mkdir(parents=True, exist_ok=True)
            source = folder / "source"
            if not source.exists():
                source.mkdir()
                for name, encoded in plan["files"].items():
                    path = safe_child(source, name)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(base64.b64decode(encoded, validate=True))
            references = folder / 'references'
            for name, encoded in plan.get('references', {}).items():
                path = safe_child(references, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(base64.b64decode(encoded, validate=True))
            settings['reference_directory'] = str(references) if plan.get('references') else None
            write(folder / "context.json", plan["context"])
            settings["timeout_seconds"] = min(900, max(1, plan["deadline"] - time.time()))
            archive_incomplete(folder / "planning", "proposal.json")
            proposal = planner.propose(settings, source, plan["context"], folder / "planning", plan["editable"])
            if proposal.get("action") == "profile":
                receipt = remote("profile", reason=proposal["profile_reason"], context_digest=proposal["context_digest"])
                write(folder / "profile-receipt.json", receipt)
                continue
            candidate = folder / "planning/candidate"
            receipt = remote("submit", proposal=proposal,
                             files={name: base64.b64encode((candidate / name).read_bytes()).decode() for name in manifest(candidate)},
                             patch=(folder / "planning/candidate.patch").read_text(),
                             events=(folder / "planning/agent/stdout.log").read_text(),
                             usage=read(folder / "planning/usage.json"))
            write(folder / "transfer-receipt.json", receipt)


if __name__ == "__main__":
    main()
