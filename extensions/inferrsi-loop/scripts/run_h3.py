"""One local command to continue H3 exploration and publish retained results."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from inferrsi_loop import reporter
from inferrsi_loop.common import write
from inferrsi_loop.configuration import launch


def main():
    parser = argparse.ArgumentParser(description="Local Codex + SSH + InferRSI; use the configured device allowlist")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--remote-config", required=True)
    parser.add_argument('--launch-config', required=True, help='Filled config/launch.template.json on the local machine')
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--codex")
    parser.add_argument("--wait-for-devices", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--no-sync", action="store_true")
    args = parser.parse_args()
    if not args.run_id.replace("-", "").replace("_", "").isalnum():
        parser.error("Use a simple run identifier")
    deployment = launch(args.launch_config)
    for key in ('model', 'effort', 'codex'):
        if getattr(args, key):
            deployment[key] = getattr(args, key)
    remote_run = deployment['remote_runs'].rstrip('/') + '/' + args.run_id
    local = Path(deployment['local_runs']) / args.run_id
    local.mkdir(parents=True, exist_ok=True)
    driver_code = 0
    if not args.report_only:
        command = [sys.executable, str(ROOT / "scripts/local_driver.py"), "--root", str(local),
                   "--remote-run", remote_run, "--remote-config", args.remote_config,
                   *[part for key in ('model', 'effort', 'codex', 'ssh', 'host', 'container', 'remote_python', 'remote_package', 'expected_user', 'lark')
                     if deployment.get(key) is not None
                     for part in ('--' + key.replace('_', '-'), deployment[key])]]
        if args.wait_for_devices:
            command += ["--wait-for-devices"]
        result = subprocess.run(command)
        driver_code = result.returncode
    command = ["docker", "exec", deployment['container'], deployment['remote_python'],
               deployment['remote_package'] + "/scripts/report_results.py", "--run", remote_run]
    response = subprocess.run([deployment['ssh'], "-T", deployment['host'], shlex.join(command)],
                              capture_output=True, text=True, check=True, timeout=90)
    report = json.loads(response.stdout)
    write(local / "results.json", report)
    write(local / "summary.json", report["summary"])
    payload = reporter.enqueue(local)
    if not args.no_sync:
        try:
            receipt = reporter.sync(local, deployment['document'], deployment['lark'])
        except Exception as exc:
            # Keep the outbox; supervisor must retry publication only.
            write(local / "sync-error.json", {"status": "pending", "error_type": type(exc).__name__})
            return 3
        print(json.dumps({"sync": receipt}, ensure_ascii=False))
        if receipt.get("status") != "synced":
            return 3
    print(json.dumps({"status": report["status"], "accepted": len(report["accepted"]),
                      "candidate_optimizations": len(report.get("candidate_optimizations", [])),
                      "pending_review": len(report["validated_pending_review"]), "results": str(local / "results.json")}, ensure_ascii=False))
    return driver_code or (2 if report["status"] in ("blocked", "finalization_incomplete", "waiting_resources") else 0)


if __name__ == "__main__":
    raise SystemExit(main())
