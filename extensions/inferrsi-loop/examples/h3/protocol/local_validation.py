"""Frozen local-test rules. No global BF16 tolerance and no random weights."""
import hashlib
import json
import math
from pathlib import Path

from support import sha


def contract_digest(contract):
    # Match inferrsi_loop.common.digest without importing the controller runtime.
    return hashlib.sha256(json.dumps(contract, sort_keys=True, allow_nan=False).encode()).hexdigest()


def verify_weights(contract):
    if contract.get("kind") != "local_module" or contract.get("weight_origin") not in ("model_capture", "checkpoint", "parameter_free"):
        raise RuntimeError("A local test requires declared real-model weight provenance")
    files = contract.get("evidence_files", [])
    if not files:
        raise RuntimeError("Missing immutable weight/input provenance")
    if contract["weight_origin"] != "parameter_free" and not any(row.get("role") in ("model_weights", "weight_capture") for row in files):
        raise RuntimeError("Real model weights are missing")
    for row in files:
        if sha(Path(row["path"])) != row["sha256"]:
            raise RuntimeError("Local test weight/input provenance changed")
    return {"contract_digest": contract_digest(contract), "weights_verified": True}


def judge(metrics, functional_ok, rule=None):
    if not functional_ok or any(not math.isfinite(metrics[k]) or metrics[k] < 0 for k in ("max_abs", "mae", "rmse", "relative_l2")):
        return "fail"
    rule = rule or {"mode": "exact_or_review"}
    mode = rule["mode"]
    if mode in ("exact", "exact_or_review"):
        if metrics["max_abs"] == 0:
            return "pass"
        return "fail" if mode == "exact" else "needs_review"
    if mode == "limits":
        limits = rule.get("limits", {})
        if not limits or not rule.get("basis"):
            raise RuntimeError("Numerical limits need a predeclared justification")
        if any(k not in ("max_abs", "mae", "rmse", "relative_l2") or type(v) not in (int, float) or not math.isfinite(v) or v < 0 for k, v in limits.items()):
            raise RuntimeError("Invalid numerical limits")
        return "pass" if all(metrics[k] <= value for k, value in limits.items()) else "fail"
    raise RuntimeError("Unknown numerical rule; use an existing module CI adapter for other criteria")


def verify_consumed_input(contract, path):
    """Verify relocated protocol input content, not its registration directory."""
    consumed = Path(path)
    fingerprint = sha(consumed)
    rows = [row for row in contract.get("evidence_files", [])
            if row.get("role") == "input" and row.get("sha256") == fingerprint]
    if not rows:
        raise RuntimeError("Consumed input does not match registered input evidence")
    return consumed
