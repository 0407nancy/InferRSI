"""Collect retained InferRSI decisions; never turn a screening gain into acceptance."""
import argparse
import json
from pathlib import Path


def collect(run):
    run = Path(run)
    summary = json.loads((run / "summary.json").read_text())
    config = json.loads((run / "frozen/config.json").read_text())
    items = []
    for folder in sorted((run / "rounds").glob("*"), key=lambda path: int(path.name)):
        if not (folder / "proposal.json").exists():
            continue
        proposal = json.loads((folder / "proposal.json").read_text())
        decisions = {}
        for path in sorted(folder.glob("evaluation/*/*/*decision.json")):
            value = json.loads(path.read_text())
            decisions[value["stage"]] = dict(value, evidence=str(path))
        feedback = json.loads((folder / 'feedback.json').read_text()) if (folder / 'feedback.json').exists() else {}
        final = decisions.get("final", {})
        registered = {row["round"]: row for row in summary.get("candidate_optimizations", [])}
        if final.get("verdict") == "PASS":
            status = "accepted" if summary["status"] == "accepted" else "validated_pending_human_review"
        elif int(folder.name) in registered:
            status = "candidate_optimization"
        elif any(value.get("reason") == "needs_numerical_review" for value in decisions.values()):
            status = "pending_numerical_review"
        elif any(value["verdict"] == "FAIL" for value in decisions.values()):
            status = "rejected"
        elif any(value["verdict"] == "INCONCLUSIVE" for value in decisions.values()):
            status = "inconclusive"
        elif decisions.get("screen", {}).get("verdict") == "PASS":
            status = "screen_gain_only"
        elif feedback.get('reason') == 'local_test_unavailable':
            status = 'unmeasured_missing_evidence_or_protocol'
        else:
            status = "awaiting_measurement"
        items.append({"round": int(folder.name), "hypothesis": proposal["hypothesis"], "status": status,
                      "rationale": proposal.get("rationale", ""),
                      "source_hash": proposal["source_hash"], "patch": str(folder / "candidate.patch"),
                      "decisions": decisions, "feedback": feedback,
                      "protocol_recovery": json.loads((folder / 'protocol-state.json').read_text()) if (folder / 'protocol-state.json').exists() else {},
                      "evidence_rounds": len(list((folder / 'supplements').glob('*/receipt.json')))})
    result = {"run_id": run.name, "run": str(run), "status": summary["status"], "scope": summary["scope"],
              "model": summary["model"], "model_execution_host": "local_mac", "summary": summary,
              "accepted": [item for item in items if item["status"] == "accepted"],
              "validated_pending_review": [item for item in items if item["status"] == "validated_pending_human_review"],
              "candidate_optimizations": summary.get("candidate_optimizations", []),
              "other_candidates": [item for item in items if item["status"] not in ("accepted", "validated_pending_human_review")]}
    result["started_at"] = json.loads((run / "state.json").read_text()).get("started_at")
    result["goal"] = config.get("goal", "")
    result["task"] = config.get("task", "explore")
    result["profiling"] = {"mode": config.get("profile", {}).get("mode", "reuse"),
                           "requests": [json.loads(p.read_text()) for p in sorted((run / "profiling").glob("request-*.json"))]}
    if result["task"] == "ablate":
        rows = []
        for p in sorted((run / "comparisons").glob("*/*/comparison.json")):
            row = json.loads(p.read_text())
            decision = json.loads(Path(row["decision"]).read_text())
            row["per_case_comparisons"] = decision.get("additional_gates", [])
            rows.append(row)
        result["ablation"] = {"plan": config["ablation"], "rows": rows, "automatic_adoption": False,
                              "interpretation": "Paired contrasts; negative gains are retained. Pairwise contrasts alone do not establish independent additive contributions."}
        plan = config["ablation"]
        selected_stage = plan.get("selection_stage", plan.get("stages", [plan.get("stage", "full")])[-1])
        result["ablation"]["selection_stage"] = selected_stage
        result["ablation"]["selected_factors"] = [r["removed"] for r in rows if r.get("removed") and r["stage"] == selected_stage and r.get("passes_selection_threshold") is True]
        result["model_calls_during_execution"] = False
    (run / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(collect(args.run), ensure_ascii=False))
