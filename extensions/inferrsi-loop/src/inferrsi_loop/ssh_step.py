"""Small JSON/stdin transport around the existing controller; no model on 93."""
import base64
import json
from pathlib import Path
import sys

from . import controller, local_tests, profiling, lifecycle, knowledge
from .common import LoopError, digest, lock, manifest, read, safe_child, write
from .planner import allowed


def dispatch(request):
    run = Path(request["run"]).resolve()
    action = request["action"]
    if action == 'configuration':
        if run.exists():
            controller.verify(run)
            return {'initialized': True, 'model': read(run/'frozen/config.json').get('model',{})}
        config = knowledge.prepare_policy(read(request['config']))
        return {'initialized': False, 'knowledge_sources': config.get('knowledge_sources', []), 'model': config.get('model',{})}
    if action == 'initialize':
        config = read(request['config'])
        if request.get('model_selection'):
            selected = request['model_selection']
            if any(not isinstance(selected.get(k),str) or not selected[k].strip() for k in ('name','effort')):
                raise LoopError('Missing resolved model/effort')
            config.setdefault('model',{}).update({k:selected[k] for k in ('name','effort')})
        controller.initialize(config, run, document_snapshots=request.get('documents'))
        return {'initialized': True}
    if action == "advance":
        if not run.exists():
            controller.initialize(read(request["config"]), run)
        return controller.run_loop(run, resume=True)
    with lock(run / ".controller.lock"):
        controller.verify(run)
        state = read(run / "state.json")
        config = read(run / "frozen/config.json")
        if action == "status":
            return {"state": state, "summary": read(run / "summary.json")}
        if action in ("test_plan", "test_submit", "test_unavailable", "test_evidence", "test_reject"):
            if state["status"] != "awaiting_local_test":
                raise LoopError("This run is not waiting for a local module test")
            folder = run / "rounds" / str(state["round"])
            if action == "test_plan":
                return local_tests.bundle(config, run, folder)
            if action == 'test_reject':
                if request['request_digest'] != digest(read(folder / 'local-test-request.json')):
                    raise LoopError('Stale rejection request')
                lifecycle.revise(config, folder, 'registration_failed', {'error': request['reason']})
                return {'status': 'retained'}
            if action == 'test_evidence':
                result = lifecycle.supplement(config, run, folder, request)
                local_tests.request(config, run, state, folder, read(folder / 'local-test-request.json')['changed_files'])
                controller.save(run, config, state)
                return result
            if action == 'test_unavailable':
                return local_tests.unavailable(config, run, folder, request, state)
            return local_tests.register(config, run, folder, request)
        if action == "profile":
            existing = list((run / "profiling").glob("request-*.json"))
            if not any(read(p)["context_digest"] == request["context_digest"] for p in existing):
                if state["status"] != "awaiting_local_proposal":
                    raise LoopError("Profiling can only be requested between experiments")
                context = read(run / "rounds" / str(state["round"]) / "context.json")
                if digest(context) != request["context_digest"]:
                    raise LoopError("Profiling request used stale context")
            return profiling.request(config, run, state, request["reason"], request["context_digest"])
        if state["status"] != "awaiting_local_proposal":
            raise LoopError("This run is not waiting for a local proposal")
        folder = run / "rounds" / str(state["round"])
        context = read(folder / "context.json")
        if action == "plan":
            source = Path(state["current_source"])
            references = {name: base64.b64encode((Path(config['reference_directory']) / name).read_bytes()).decode()
                          for name in manifest(config['reference_directory'])} if config.get('reference_directory') else {}
            if config.get('knowledge_directory'):
                root = Path(config['knowledge_directory'])
                references.update({'project-knowledge/' + name: base64.b64encode((root/name).read_bytes()).decode()
                                   for name in manifest(root) if not name.startswith('documents/')})
            own = run / 'knowledge'
            if own.exists():
                references.update({'own-feedback/' + name: base64.b64encode((own/name).read_bytes()).decode()
                                   for name in manifest(own)})
            return {"round": state["round"], "context": context, "editable": config["project"]["editable"],
                    "deadline": state["deadline"], "model": config["model"],
                    "references": references,
                    "files": {name: base64.b64encode((source / name).read_bytes()).decode() for name in manifest(source)}}
        if action != "submit":
            raise LoopError("Unknown SSH step")
        proposal = request["proposal"]
        if proposal["context_digest"] != digest(context):
            raise LoopError("The local proposal used a different context")
        candidate = folder / "candidate"
        if (folder / "proposal.json").exists():
            if read(folder / "proposal.json")["source_hash"] != proposal["source_hash"]:
                raise LoopError("This round already has a different candidate")
            return {"status": "recorded"}
        if candidate.exists():
            raise LoopError("Unsettled candidate upload; inspect before retry")
        old = manifest(state["current_source"])
        import hashlib
        contents = {name: base64.b64decode(value, validate=True) for name, value in request["files"].items()}
        after = {name: hashlib.sha256(value).hexdigest() for name, value in contents.items()}
        changed = {name for name in old.keys() | after.keys() if old.get(name) != after.get(name)}
        if not changed or any(not allowed(name, config["project"]["editable"]) for name in changed):
            raise LoopError("Uploaded candidate changed undeclared files")
        if changed != set(proposal["changed_files"]) or digest(after) != proposal["source_hash"]:
            raise LoopError("Candidate transport fingerprint mismatch")
        # Validate every path before creating any files.
        for name in contents:
            safe_child(candidate, name)
        candidate.mkdir()
        for name, value in contents.items():
            path = safe_child(candidate, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        (folder / "candidate.patch").write_text(request["patch"])
        (folder / "agent-events.jsonl").write_text(request["events"])
        write(folder / "usage.json", request["usage"])
        write(folder / "proposal.json", dict(proposal, model_execution_host="local_mac"))
        return {"status": "recorded", "source_hash": proposal["source_hash"]}


def main():
    try:
        print(json.dumps(dispatch(json.load(sys.stdin)), ensure_ascii=False))
    except (LoopError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
