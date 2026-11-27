from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import adapter, controller, reporter, runner
from .common import LoopError, read


def main():
    parser = argparse.ArgumentParser(description="Codex exploration backed by frozen InferRSI campaigns")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("start", "doctor"):
        child = sub.add_parser(name)
        child.add_argument("--config", type=Path, required=True)
        child.add_argument("--run", type=Path, required=True)
        if name == "doctor":
            child.add_argument("--probe-model", action="store_true")
        else:
            child.add_argument("--prepare-only", action="store_true")
    for name in ("resume", "status", "sync"):
        child = sub.add_parser(name)
        child.add_argument("--run", type=Path, required=True)
        if name == "sync":
            child.add_argument("--document", required=True)
            child.add_argument("--binary", default="lark-cli")
    child = sub.add_parser("run-job")
    child.add_argument("--job", type=Path, required=True)
    child.add_argument("--output", type=Path, required=True)
    sub.add_parser("devices")
    args = parser.parse_args()
    try:
        if args.action == "start":
            run = controller.initialize(read(args.config), args.run)
            result = {"status": "prepared", "run": str(run)} if args.prepare_only else controller.run_loop(run)
        elif args.action == "resume":
            result = controller.run_loop(args.run, resume=True)
        elif args.action == "status":
            result = read(args.run / "state.json")
        elif args.action == "doctor":
            result = adapter.doctor(read(args.config)["model"], args.run / "adapter", probe_model=args.probe_model)
        elif args.action == "run-job":
            job = read(args.job)
            with runner.resources(job.get("resources", {}), args.output) as env:
                result = runner.execute(job, args.output, env=env)
        elif args.action == "sync":
            result = reporter.sync(args.run, args.document, args.binary)
        else:
            result = runner.probe()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (LoopError, ValueError, OSError, KeyError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
