"""Replay the real H3 TP2 MLP projection through the candidate plugin."""
import argparse
import os
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace

from support import load, save, sha
from local_validation import judge, verify_weights


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--action", choices=("quality", "measure", "numerical", "profile"), required=True)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    spec = load(args.spec)
    contract = spec["local_validation"]
    validation = verify_weights(contract)
    project = args.project.resolve()
    sys.path.insert(0, str(project))
    import torch
    import torch.distributed as dist
    import torch.nn.functional as functional
    import vllm
    import vllm_omni_kunlun
    actual = Path(vllm_omni_kunlun.__file__).resolve()
    if not actual.is_relative_to(project):
        raise RuntimeError("Candidate plugin was not imported")
    from vllm_omni_kunlun.models.minimax_h3.projection_pipeline import project as projection
    rank = int(os.environ["LOCAL_RANK"])
    if int(os.environ["WORLD_SIZE"]) != 2:
        raise RuntimeError("The captured H3 projection requires TP2")
    torch.cuda.set_device(rank)
    torch.set_num_threads(2)
    dist.init_process_group("nccl")
    rows = []
    class UnquantizedLinearMethod:
        def apply(self, layer, x, bias):
            return functional.linear(x, layer.weight, bias)
    try:
        with torch.inference_mode():
            for case, sources in spec["captures"].items():
                entry = sources[rank]
                if sha(entry["path"]) != entry["sha256"]:
                    raise RuntimeError("Captured numerical input changed")
                data = torch.load(entry["path"], map_location="cpu", weights_only=True)
                x = data["x"].cuda()
                layer = SimpleNamespace(weight=data["weight"].cuda(), bias=None, input_is_parallel=True,
                                        reduce_results=True, tp_size=2, return_bias=True,
                                        quant_method=UnquantizedLinearMethod())
                def original(value):
                    result = functional.linear(value, layer.weight)
                    dist.all_reduce(result)
                    return result, None
                def run():
                    return projection(layer, x, chunks=spec["projection_chunks"], original=original, group=None)[0]
                row = {"case": case, "shape": list(x.shape), "input_sha256": entry["sha256"]}
                if args.action in ("quality", "numerical"):
                    output = run()
                    ok = output.shape == (x.shape[0], layer.weight.shape[0]) and output.dtype == x.dtype and bool(output.isfinite().all())
                    flag = torch.tensor(int(ok), device="cuda")
                    dist.all_reduce(flag, op=dist.ReduceOp.MIN)
                    row["functional_ok"] = bool(flag.item())
                    if row["functional_ok"]:
                        reference = original(x)[0]
                        sum_square = ref_square = absolute = maximum = 0.0
                        for actual_part, reference_part in zip(output.flatten().split(1048576), reference.flatten().split(1048576)):
                            delta = actual_part.float() - reference_part.float()
                            sum_square += delta.square().sum().item()
                            ref_square += reference_part.float().square().sum().item()
                            absolute += delta.abs().sum().item()
                            maximum = max(maximum, delta.abs().max().item())
                        row["numerical"] = {"max_abs": maximum, "mae": absolute / output.numel(),
                                            "rmse": (sum_square / output.numel()) ** .5,
                                            "relative_l2": (sum_square / max(ref_square, 1e-30)) ** .5,
                                            "reference": "original F.linear + TP2 all_reduce on identical captured tensors",
                                            "coverage": "entire output"}
                        status = judge(row["numerical"], row["functional_ok"], contract.get("numerical_rule"))
                        del reference
                    else:
                        status = "fail"
                    # Every rank participates in the decision; rank 0 alone cannot pass.
                    verdict = torch.tensor({"pass": 0, "needs_review": 1, "fail": 2}[status], device="cuda")
                    dist.all_reduce(verdict, op=dist.ReduceOp.MAX)
                    row["numerical_status"] = {0: "pass", 1: "needs_review", 2: "fail"}[int(verdict.item())]
                    del output
                else:
                    samples = []
                    warmups = spec.get("warmups", 2)
                    measurements = spec.get("measurements", 3)
                    for index in range(warmups + measurements):
                        dist.barrier()
                        torch.cuda.synchronize()
                        started = time.perf_counter()
                        output = run()
                        torch.cuda.synchronize()
                        elapsed = torch.tensor((time.perf_counter() - started) * 1000, device="cuda")
                        dist.all_reduce(elapsed, op=dist.ReduceOp.MAX)
                        if index >= warmups:
                            samples.append(elapsed.item())
                        del output
                    row.update(samples_ms=samples, median_ms=statistics.median(samples))
                rows.append(row)
                del x, layer, data
                torch.cuda.empty_cache()
            result = {"scope": "h3_tp2_mlp_projection_replay", "rank": rank, "rows": rows,
                      "model_quality_accepted": False, "candidate_plugin": str(actual), "local_validation": validation}
            if args.action in ("quality", "numerical"):
                result["functional_ok"] = all(row["functional_ok"] for row in rows)
                statuses = {row["numerical_status"] for row in rows}
                status = "fail" if "fail" in statuses else "needs_review" if "needs_review" in statuses else "pass"
                validation["numerical_status"] = status
                if status != "needs_review":
                    result["numerical_ok"] = status == "pass"
                else:
                    result["reason"] = "numerical_rule_required; retain errors, do not count this as a candidate or failure"
            else:
                result.update(latency_ms=sum(row["median_ms"] for row in rows), warmups=spec.get("warmups", 2), measurements=spec.get("measurements", 3))
            save(args.result.with_name(f"rank{rank}.json"), result)
            if rank == 0:
                save(args.result, result)
    finally:
        dist.destroy_process_group()


if __name__ == "__main__":
    main()
