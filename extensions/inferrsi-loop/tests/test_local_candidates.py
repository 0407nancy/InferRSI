"""CPU orchestration fixtures, not MiniMax-H3 performance/accuracy evidence."""
import base64
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from inferrsi_loop import controller, evaluator, local_tests, ssh_step
from inferrsi_loop.common import copy_inputs, digest, manifest, read, sha, write
import test_task_modes
from test_loop import ROOT, stage


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class NumericalRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / 'examples/h3/protocol'))
        cls.rules = module('local_rules_fixture', ROOT / 'examples/h3/protocol/local_validation.py')

    def test_nonzero_error_without_rule_is_pending_not_failure(self):
        values = dict(max_abs=0.01, mae=0.001, rmse=0.002, relative_l2=0.0001)
        self.assertEqual(self.rules.judge(values, True), 'needs_review')
        self.assertEqual(self.rules.judge(dict.fromkeys(values, 0), True), 'pass')
        self.assertEqual(self.rules.judge(values, False), 'fail')
        self.assertEqual(self.rules.judge(values, True, {'mode': 'exact'}), 'fail')
        self.assertEqual(self.rules.judge(values, True, {'mode': 'limits', 'limits': {'max_abs': .02}, 'basis': 'unit-test fixture only'}), 'pass')
        self.assertEqual(self.rules.judge(values, True, {'mode': 'limits', 'limits': {'max_abs': .005}, 'basis': 'unit-test fixture only'}), 'fail')
        with self.assertRaises(RuntimeError):
            self.rules.judge(values, True, {'mode': 'limits', 'limits': {'max_abs': .02}})

    def test_fake_or_changed_weight_provenance_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            weights = Path(folder) / 'cpu-provenance-fixture'
            weights.write_bytes(b'fixture, not actual model weights')
            contract = {'kind': 'local_module', 'weight_origin': 'model_capture',
                        'evidence_files': [{'path': str(weights), 'sha256': sha(weights), 'role': 'weight_capture'}]}
            self.assertEqual(self.rules.verify_weights(contract)['contract_digest'], digest(contract))
            with self.assertRaises(RuntimeError):
                self.rules.verify_weights(dict(contract, weight_origin='random'))
            weights.write_bytes(b'changed')
            with self.assertRaises(RuntimeError):
                self.rules.verify_weights(contract)

    def test_copied_input_is_verified_by_content_not_original_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, copied = root / 'registered.json', root / 'copied.json'
            original.write_bytes(b'{"fixed": 1}')
            copied.write_bytes(original.read_bytes())
            contract = {'evidence_files': [{'path': str(original), 'role': 'input', 'sha256': sha(original)}]}
            self.assertEqual(self.rules.verify_consumed_input(contract, copied), copied)
            copied.write_bytes(b'{"fixed": 2}')
            with self.assertRaises(RuntimeError):
                self.rules.verify_consumed_input(contract, copied)
            copied.write_bytes(original.read_bytes())
            contract['evidence_files'][0]['role'] = 'weight_capture'
            with self.assertRaises(RuntimeError):
                self.rules.verify_consumed_input(contract, copied)

    def test_missing_or_pending_numerical_proof_never_passes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            contract = {'kind': 'local_module'}
            observed = {'path': 'attempt', 'execution': {'steps': [{'id': 'quality-baseline'}, {'id': 'quality-candidate'}]}}
            proof = {'contract_digest': digest(contract), 'weights_verified': True, 'numerical_status': 'pass'}
            for side in ('baseline', 'candidate'):
                write(root / f'attempt/execution/quality-{side}/result.json', {'numerical_ok': True, 'local_validation': proof})
            self.assertEqual(evaluator.verify_local_evidence(root, observed, contract), 'verified')
            write(root / 'attempt/execution/quality-candidate/result.json', {'functional_ok': True})
            with self.assertRaises(LoopError):
                evaluator.verify_local_evidence(root, observed, contract)
            write(root / 'attempt/execution/quality-candidate/result.json', {'local_validation': dict(proof, numerical_status='needs_review')})
            self.assertEqual(evaluator.verify_local_evidence(root, observed, contract), 'needs_numerical_review')


from inferrsi_loop.common import LoopError


class LocalCandidateTests(unittest.TestCase):
    setUp = test_task_modes.TaskModesTests.setUp
    tearDown = test_task_modes.TaskModesTests.tearDown
    start = test_task_modes.TaskModesTests.start

    def configure(self):
        protocol = self.root / 'protocol'
        copy_inputs(ROOT / 'examples/cpu/protocol', protocol, list(manifest(ROOT / 'examples/cpu/protocol')))
        self.contract = {'kind': 'local_module', 'module': 'CPU harness fixture only'}
        text = (protocol / 'check.py').read_text()
        # Attach test-only proof to the existing real InferRSI CPU fixture.
        text += "\np=__import__('pathlib').Path(a.result);d=__import__('json').loads(p.read_text());d['local_validation']=" + repr({'contract_digest': digest(self.contract), 'weights_verified': True}) + "\n"
        text += "if a.action=='quality':\n d['numerical_ok']=d['functional_ok'];d['local_validation']['numerical_status']='pass' if d['functional_ok'] else 'fail'\n"
        text += "p.write_text(__import__('json').dumps(d))\n"
        (protocol / 'check.py').write_text(text)
        self.cfg['protocols']['screen'] = dict(stage(protocol), quality_key='numerical_ok', local_validation=self.contract)
        self.cfg.update(enable_full=True, exploration={'validation': 'local_only'})
        self.cfg['budget'].update(max_rounds=2, max_seconds=300)

    def submit(self, source, name='candidate.py'):
        context = read(self.run / f"rounds/{read(self.run / 'state.json')['round']}/context.json")
        candidate = self.root / 'upload'
        candidate.mkdir(exist_ok=True)
        target = candidate / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
        ssh_step.dispatch({'action': 'submit', 'run': str(self.run),
            'files': {name: base64.b64encode(source.encode()).decode()},
            'proposal': {'context_digest': digest(context), 'source_hash': digest(manifest(candidate)),
                         'changed_files': [name], 'hypothesis': 'CPU orchestration fixture'},
            'patch': 'CPU fixture only', 'events': '', 'usage': {}})

    def test_real_local_candidate_does_not_run_e2e_or_change_next_baseline(self):
        self.configure()
        self.start()
        self.submit('from collections import Counter\ndef histogram(values): return dict(Counter(values))\n')
        result = controller.run_loop(self.run, resume=True)
        self.assertEqual(result['status'], 'awaiting_local_proposal', result)
        self.assertEqual(len(result['candidate_optimizations']), 1, result)
        self.assertEqual(result['candidate_optimizations'][0]['status'], 'candidate_optimization')
        self.assertEqual(read(self.run / 'state.json')['current_source'], str(self.run / 'frozen/baseline'))
        self.assertEqual({row['stage'] for row in result['history']}, {'screen'})
        self.submit('def histogram(values): return {}\n')
        result = controller.run_loop(self.run, resume=True)
        self.assertEqual(result['status'], 'screen_only_complete', result)
        self.assertEqual(len(result['candidate_optimizations']), 1)
        self.assertFalse(result['full_validation_accepted'])
        report = module('report_fixture', ROOT / 'scripts/report_results.py').collect(self.run)
        self.assertEqual(len(report['candidate_optimizations']), 1)
        self.assertEqual(report['accepted'], [])

    def test_pending_numerical_review_is_retained_and_next_exploration_continues(self):
        self.configure()
        self.start()
        self.submit('def histogram(values): return {}\n')
        with patch.object(evaluator, 'assess', return_value={'verdict': 'INCONCLUSIVE', 'reason': 'needs_numerical_review', 'assessment': {}}):
            result = controller.run_loop(self.run, resume=True)
        self.assertEqual(result['status'], 'awaiting_local_proposal')
        self.assertEqual(result['completed_rounds'], 1)
        self.assertEqual(result['candidate_optimizations'], [])

    def test_new_candidate_source_file_reaches_real_execution_snapshot(self):
        self.configure()
        self.cfg['project']['editable'] = ['.']
        self.cfg['budget']['max_rounds'] = 1
        self.start()
        candidate = self.root / 'with-new-source'
        candidate.mkdir()
        (candidate / 'candidate.py').write_text("from pathlib import Path\nfrom collections import Counter\nassert Path(__file__).with_name('added.txt').read_text() == 'present'\ndef histogram(values):return dict(Counter(values))\n")
        (candidate / 'added.txt').write_text('present')
        context = read(self.run / 'rounds/1/context.json')
        ssh_step.dispatch({'action': 'submit', 'run': str(self.run),
            'files': {n: base64.b64encode((candidate/n).read_bytes()).decode() for n in manifest(candidate)},
            'proposal': {'context_digest': digest(context), 'source_hash': digest(manifest(candidate)),
                         'changed_files': ['candidate.py', 'added.txt'], 'hypothesis': 'new source transport fixture'},
            'patch': 'CPU fixture', 'events': '', 'usage': {}})
        result = controller.run_loop(self.run, resume=True)
        self.assertEqual(result['status'], 'screen_only_complete', result)
        self.assertEqual(len(result['candidate_optimizations']), 1, result)

    def test_uncovered_module_cannot_fall_back_or_be_registered(self):
        self.configure()
        self.cfg['screen_routes'] = [{'paths': ['different.py'], 'protocol': 'screen'}]
        self.start()
        self.submit('def histogram(values): return {}\n')
        with patch.object(evaluator, 'assess', side_effect=AssertionError('Unrelated test cannot run')):
            result = controller.run_loop(self.run, resume=True)
        self.assertEqual(result['status'], 'awaiting_local_test')
        self.assertEqual(result['reason'], 'local_codex_prepare_module_test')
        self.assertEqual(result['candidate_optimizations'], [])

    def test_missing_local_fixture_is_retained_without_measurement_and_search_continues(self):
        self.configure()
        self.cfg['screen_routes'] = [{'paths': ['different.py'], 'protocol': 'screen'}]
        self.start()
        self.submit('def histogram(values): return {}\n')
        controller.run_loop(self.run, resume=True)
        plan = ssh_step.dispatch({'action': 'test_plan', 'run': str(self.run)})
        payload = {'action': 'test_unavailable', 'run': str(self.run),
                   'request_digest': digest(plan['request']), 'reason': 'No applicable real checkpoint fixture'}
        with self.assertRaises(LoopError):
            ssh_step.dispatch(dict(payload, request_digest='stale'))
        ssh_step.dispatch(payload)
        with patch.object(evaluator, 'assess', side_effect=AssertionError('No test may run')):
            result = controller.run_loop(self.run, resume=True)
        self.assertEqual(result['status'], 'awaiting_local_proposal')
        self.assertEqual(result['completed_rounds'], 1)
        self.assertEqual(result['candidate_optimizations'], [])
        self.assertFalse(result['history'][0]['assessment']['measured'])
        self.assertEqual(result['history'][0]['verdict'], 'INCONCLUSIVE')

    def test_recovery_enabled_protocol_preflight_then_real_campaign(self):
        original = self.configure
        def configure_with_recovery():
            original()
            self.cfg['local_recovery'] = {'enabled': True, 'max_protocol_versions': 3}
        self.configure = configure_with_recovery
        self.test_non_mlp_module_prepares_freezes_and_executes_local_test()
        summary = read(self.run / 'summary.json')
        self.assertEqual(summary['candidate_count'], 1)
        self.assertEqual(summary['effective_experiments'], 1)
        self.assertTrue(read(self.run / 'rounds/1/protocol-versions/v01/preflight.json')['passed'])

    def test_non_mlp_module_prepares_freezes_and_executes_local_test(self):
        self.configure()
        project = self.root / 'project'
        (project / 'ops').mkdir(parents=True)
        (project / 'ops/attention.py').write_bytes((ROOT / 'examples/cpu/project/candidate.py').read_bytes())
        self.cfg['project'].update(path=str(project), include=['ops/attention.py'], editable=['.'])
        self.cfg['screen_routes'] = [{'paths': ['mlp.py'], 'protocol': 'screen'}]
        self.cfg['budget']['max_rounds'] = 1
        self.start()
        self.submit('from collections import Counter\ndef histogram(values): return dict(Counter(values))\n', 'ops/attention.py')
        result = controller.run_loop(self.run, resume=True)
        self.assertEqual(result['status'], 'awaiting_local_test')
        plan = ssh_step.dispatch({'action': 'test_plan', 'run': str(self.run)})
        self.assertEqual(plan['request']['changed_files'], ['ops/attention.py'])
        data = self.root / 'cpu-fixture.json'
        write(data, {'purpose': 'CPU orchestration fixture, not actual H3 attention'})
        contract = {'kind': 'local_module', 'module': 'non_mlp_cpu_fixture', 'exercised_symbols': ['histogram'],
                    'weight_origin': 'parameter_free', 'evidence_files': [{'path': '{protocol}/input.json', 'role': 'input'}],
                    'numerical_rule': {'mode': 'exact_or_review'}}
        protocol = self.root / 'generated-protocol'
        protocol.mkdir()
        (protocol / 'input.json').write_bytes(data.read_bytes())
        spec = {'paths': ['ops/attention.py'], 'scope': 'non_mlp_cpu_fixture',
                'metric_key': 'latency_ms', 'metric_unit': 'ms', 'direction': 'minimize', 'local_validation': contract,
                'resources': {'count': 8}, 'min_improvement_pct': -99}
        for action in ('quality', 'measure'):
            spec[action + '_command'] = ['{python}', '{protocol}/check.py', '--action', action, '--project', '{candidate}', '--result', '{result}']
        write(protocol / 'adapter.json', spec)
        (protocol / 'instructions.txt').write_text('CPU fixture only')
        code = (ROOT / 'examples/cpu/protocol/check.py').read_text().replace('Path(a.project) / "candidate.py"', 'Path(a.project) / "ops/attention.py"')
        code += "\ncontract=json.loads(Path(__file__).with_name('adapter.json').read_text())['local_validation']\n"
        code += "result['local_validation']={'contract_digest':__import__('hashlib').sha256(json.dumps(contract,sort_keys=True,allow_nan=False).encode()).hexdigest(),'weights_verified':True}\n"
        code += "if a.action=='quality':\n result['numerical_ok']=result['functional_ok'];result['local_validation']['numerical_status']='pass' if result['functional_ok'] else 'fail'\n"
        code += "out.write_text(json.dumps(result))\n"
        (protocol / 'check.py').write_text(code)
        payload = {'action': 'test_submit', 'run': str(self.run), 'request_digest': digest(plan['request']),
                   'files': {n: base64.b64encode((protocol / n).read_bytes()).decode() for n in manifest(protocol)}}
        wrong = dict(payload, request_digest='stale')
        with self.assertRaises(LoopError):
            ssh_step.dispatch(wrong)
        receipt = ssh_step.dispatch(payload)
        self.assertEqual(receipt['protocol']['resources']['count'], 0)
        self.assertEqual(receipt['protocol']['min_improvement_pct'], 0)
        self.assertEqual(receipt, ssh_step.dispatch(payload))
        self.assertEqual(receipt['protocol']['local_validation']['evidence_files'][0]['sha256'], sha(data))
        result = controller.run_loop(self.run, resume=True)
        self.assertEqual(result['status'], 'screen_only_complete', result)
        self.assertEqual(result['candidate_optimizations'][0]['protocol'], 'local_module')
        self.assertFalse((self.run / 'rounds/1/evaluation/full').exists())
        target = Path(read(self.run / 'rounds/1/local-test-receipt.json')['protocol']['directory']) / 'check.py'
        target.write_text('changed')
        with self.assertRaisesRegex(LoopError, 'Frozen local test'):
            local_tests.selected(read(self.run / 'frozen/config.json'), self.run, self.run / 'rounds/1/candidate')


class LocalTestPreparationTests(unittest.TestCase):
    def test_local_agent_preparation_is_cached_and_does_not_edit_reference(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan = {'request': {'round': 1, 'changed_files': ['ops/attention.py']},
                    'files': {'baseline/ops/attention.py': base64.b64encode(b'original=1').decode(),
                              'candidate/ops/attention.py': base64.b64encode(b'candidate=1').decode()}}
            def fake_model(argv, cwd, output, *args, **kwargs):
                (cwd / 'protocol').mkdir()
                (cwd / 'protocol/instructions.txt').write_text('Test-only mocked model output')
                write(Path(argv[-1]), {'ready': True, 'summary': 'CPU fixture only', 'reason': ''})
                return {'status': 'completed'}
            with patch('inferrsi_loop.adapter.command', side_effect=lambda s, w, schema, answer: ['fake', str(answer)]), \
                 patch('inferrsi_loop.common.run_command', side_effect=fake_model) as called:
                result = local_tests.prepare({'name': 'fixture'}, plan, root / 'first')
                self.assertEqual(local_tests.prepare({'name': 'fixture'}, plan, root / 'first'), result)
                self.assertEqual(called.call_count, 1)
                self.assertIn('no e2e', called.call_args.kwargs['stdin'].lower())
            def modifies_reference(argv, cwd, output, *args, **kwargs):
                result = fake_model(argv, cwd, output, *args, **kwargs)
                (cwd / 'baseline/ops/attention.py').write_text('changed')
                return result
            with patch('inferrsi_loop.adapter.command', side_effect=lambda s, w, schema, answer: ['fake', str(answer)]), \
                 patch('inferrsi_loop.common.run_command', side_effect=modifies_reference):
                with self.assertRaisesRegex(LoopError, 'modified its reference'):
                    local_tests.prepare({'name': 'fixture'}, plan, root / 'second')
