"""Frozen native-protocol entry: validate evidence before InferRSI reads results."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def main():
    p = argparse.ArgumentParser()
    for name in ('action', 'candidate', 'baseline', 'result', 'python'):
        p.add_argument('--' + name, required=True)
    a = p.parse_args()
    root = Path(__file__).resolve().parent
    spec = json.loads((root / '_evidence_gate.json').read_text())
    values = dict(vars(a), protocol=str(root))
    result = subprocess.run([part.format(**values) for part in spec[a.action + '_command']])
    if result.returncode:
        return result.returncode
    value = json.loads(Path(a.result).read_text())
    contract = spec['local_validation']
    proof = value.get('local_validation', {})
    if proof.get('contract_digest') != digest(contract) or proof.get('weights_verified') is not True:
        raise ValueError('Missing/stale real-weight or fixed-input provenance')
    if a.action == 'measure':
        plan = contract.get('measurement_plan', {})
        samples = value.get('samples', [])
        if plan and (any(value.get(k) != plan[k] for k in ('warmups', 'measurements'))
                     or len(samples) != plan['measurements']
                     or any(type(x) not in (int, float) or not math.isfinite(x) or x <= 0 for x in samples)):
            raise ValueError('Measurement differs from frozen plan')
    else:
        state = proof.get('numerical_status')
        if state == 'needs_review':
            value.pop('numerical_ok', None)  # Native INCONCLUSIVE; never a quality pass.
            Path(a.result).write_text(json.dumps(value, allow_nan=False))
        elif state not in ('pass', 'fail') or type(value.get('numerical_ok')) is not bool or value['numerical_ok'] != (state == 'pass'):
            raise ValueError('Missing or inconsistent module numerical evidence')
    coverage = contract.get('coverage', {})
    if coverage:
        expected = {c['id'] for c in contract['cases']}
        cases = value.get('cases', [])
        if len(cases) != len(expected) or {c.get('id') for c in cases} != expected:
            raise ValueError('Every frozen workload case must have retained evidence')
        for case in cases:
            if a.action == 'quality' and case.get('numerical_status') not in ('pass', 'fail', 'needs_review'):
                raise ValueError('Case numerical comparison missing')
            if a.action == 'measure' and case.get('timed', True):
                samples = case.get('samples', [])
                plan = contract['measurement_plan']
                if len(samples) != plan['measurements'] or any(type(x) not in (int,float) or not math.isfinite(x) or x <= 0 for x in samples):
                    raise ValueError('Case timing samples missing')
        if a.action == 'quality' and value.get('numerical_ok') is True and any(c['numerical_status'] != 'pass' for c in cases):
            raise ValueError('Aggregate quality pass conflicts with a workload case')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
