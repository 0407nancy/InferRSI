import base64
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from inferrsi_loop import controller, profiling, ssh_step, planner
from inferrsi_loop.common import LoopError, digest, manifest, read, write
from test_loop import ROOT, stage


class TaskModesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        evidence = self.root / 'old.json'
        write(evidence, {'marker': 'old measured evidence'})
        self.collector = self.root / 'collector.py'
        self.collector.write_text("import json,sys\nfrom pathlib import Path\np=Path(sys.argv[1]);p.mkdir(exist_ok=True);(p/'summary.json').write_text(json.dumps({'marker':'fresh evidence'}))\n")
        self.cfg = {'task': 'explore', 'model': {'name': 'example', 'transport': 'local_ssh'}, 'goal': 'fixture',
                    'project': {'path': str(ROOT / 'examples/cpu/project'), 'include': ['candidate.py'], 'editable': ['candidate.py']},
                    'profile': {'mode': 'reuse', 'evidence': str(evidence), 'bottleneck': 'fixture'},
                    'protocols': {'screen': stage(ROOT / 'examples/cpu/protocol')}, 'enable_full': False,
                    'budget': {'max_rounds': 2, 'max_seconds': 180}, 'human_review_required': False}

    def tearDown(self):
        self.temp.cleanup()

    def collector_config(self, mode):
        self.cfg['profile'].update(mode=mode, argv=[sys.executable, str(self.collector), '{output}'],
                                   timeout_seconds=10, resources={'count': 0}, max_refreshes=1)

    def start(self):
        self.run = controller.initialize(self.cfg, self.root / 'run')
        return controller.run_loop(self.run)

    def test_reuse_never_runs_collector_and_refuses_refresh(self):
        self.collector_config('reuse')
        with patch('inferrsi_loop.runner.execute', side_effect=AssertionError('Collection was forbidden')):
            self.start()
        context = read(self.run / 'rounds/1/context.json')
        self.assertIn('old measured evidence', context['profile_excerpt'])
        with self.assertRaises(LoopError):
            ssh_step.dispatch({'action': 'profile', 'run': str(self.run), 'reason': 'want new data', 'context_digest': digest(context)})
        self.assertFalse((self.run / 'jobs').exists())

    def test_collect_ignores_old_evidence_and_resume_does_not_reserve_or_repeat(self):
        self.collector_config('collect')
        self.start()
        context = read(self.run / 'rounds/1/context.json')
        self.assertIn('fresh evidence', context['profile_excerpt'])
        process = read(self.run / 'jobs/profile-000/execution/process.json')
        with patch('inferrsi_loop.runner.resources', side_effect=AssertionError('Completed profiling needs no cards')):
            controller.run_loop(self.run, resume=True)
        self.assertEqual(process, read(self.run / 'jobs/profile-000/execution/process.json'))

    def test_adaptive_feedback_budget_and_transport_dedup(self):
        self.collector_config('adaptive')
        self.start()
        old = read(self.run / 'rounds/1/context.json')
        req = {'action': 'profile', 'run': str(self.run), 'reason': 'Need fresh gaps after runtime upgrade', 'context_digest': digest(old)}
        first = ssh_step.dispatch(req)
        self.assertEqual(first, ssh_step.dispatch(req))
        self.assertEqual(controller.run_loop(self.run, resume=True)['status'], 'awaiting_local_proposal')
        fresh = read(self.run / 'rounds/1/context.json')
        self.assertEqual(fresh['profile']['generation'], 1)
        self.assertEqual(fresh['profile']['refreshes_remaining'], 0)
        self.assertIn('fresh evidence', fresh['profile_excerpt'])
        self.assertEqual(first, ssh_step.dispatch(req))
        with self.assertRaises(LoopError):
            ssh_step.dispatch(dict(req, context_digest=digest(fresh)))

    def test_local_only_rejects_short_e2e_fallback(self):
        self.cfg['project']['editable'] = ['.']
        self.cfg['protocols']['short'] = dict(self.cfg['protocols']['screen'], scope='short_e2e_fixture')
        self.cfg['screen_routes'] = [{'paths': ['.'], 'protocol': 'short'}]
        self.cfg['exploration'] = {'validation': 'local_only'}
        with self.assertRaisesRegex(LoopError, 'E2E fallback is forbidden'):
            controller.initialize(self.cfg, self.root / 'bad')

    def test_missing_collection_evidence_blocks_exploration(self):
        self.collector_config('collect')
        self.collector.write_text('pass\n')
        result = self.start()
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse((self.run / 'rounds/1/context.json').exists())

    def test_collected_baseline_with_external_activity_is_not_reused(self):
        self.collector_config('collect')
        self.start()
        write(self.run / 'jobs/profile-000/occupancy.json', {'external_activity': True, 'probe_failed': False})
        result = controller.run_loop(self.run, resume=True)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('isolation was invalid', result['reason'])

    def test_ablation_rejects_unknown_variant_before_running(self):
        self.cfg.update(task='ablate', profile={'mode': 'skip'}, ablation={'stage': 'screen', 'variants': [],
                         'comparisons': [{'reference': 'baseline', 'candidate': 'missing'}]})
        with self.assertRaises(LoopError):
            controller.initialize(self.cfg, self.root / 'bad')

    def test_ablation_rejects_out_of_scope_changes(self):
        source = self.root / 'source'
        source.mkdir()
        (source / 'candidate.py').write_text('x=1')
        (source / 'gate.py').write_text('x=2')
        self.cfg['project']['path'] = str(source)
        self.cfg['project']['include'] = ['candidate.py', 'gate.py']
        variant = self.root / 'variant'
        variant.mkdir()
        (variant / 'candidate.py').write_text('x=1')
        (variant / 'gate.py').write_text('x=3')
        self.cfg.update(task='ablate', profile={'mode': 'skip'}, ablation={'stage': 'screen',
                        'variants': [{'name': 'A', 'source': str(variant), 'enabled': ['A']}],
                        'comparisons': [{'reference': 'baseline', 'candidate': 'A'}]})
        with self.assertRaises(LoopError):
            controller.initialize(self.cfg, self.root / 'bad')

    def test_model_profile_request_cannot_smuggle_source_changes(self):
        self.collector_config('adaptive')
        self.start()
        context = read(self.run / 'rounds/1/context.json')
        def fake_model(argv, cwd, output, *a, **kw):
            answer = Path(argv[argv.index('--output-last-message') + 1])
            (cwd / 'candidate.py').write_text('modified = True\n')
            write(answer, {'action': 'profile', 'profile_reason': 'Need gaps', 'hypothesis': 'inspect', 'rationale': 'fixture',
                           'changed_files': [], 'test_suggestions': []})
            return {'status': 'completed'}
        with patch.object(planner, 'run_command', side_effect=fake_model), patch.object(planner, 'command',
                side_effect=lambda settings, candidate, schema, answer: ['fake', '--output-last-message', str(answer)]):
            with self.assertRaises(LoopError):
                planner.propose({'name': 'fixture'}, self.run / 'frozen/baseline', context, self.root / 'planning', ['candidate.py'])

    def test_profile_request_does_not_need_an_optimization_hypothesis(self):
        self.collector_config('adaptive')
        self.start()
        context = read(self.run / 'rounds/1/context.json')
        def fake_model(argv, cwd, output, *a, **kw):
            answer = Path(argv[argv.index('--output-last-message') + 1])
            write(answer, {'action': 'profile', 'profile_reason': 'Need gaps', 'hypothesis': '', 'rationale': 'fixture',
                           'changed_files': [], 'test_suggestions': []})
            return {'status': 'completed'}
        with patch.object(planner, 'run_command', side_effect=fake_model), patch.object(planner, 'command',
                side_effect=lambda settings, candidate, schema, answer: ['fake', '--output-last-message', str(answer)]):
            result = planner.propose({'name': 'fixture'}, self.run / 'frozen/baseline', context, self.root / 'planning', ['candidate.py'])
        self.assertEqual(result['action'], 'profile')


class AblationIntegrationTests(unittest.TestCase):
    setUp = TaskModesTests.setUp
    tearDown = TaskModesTests.tearDown
    start = TaskModesTests.start
    # Reuse fixture setup only; integration asserts real InferRSI measurements.
    def test_fixed_ablation_retains_fast_and_slow_contrasts_without_planner(self):
        fast = self.root / 'fast'
        fast.mkdir()
        (fast / 'candidate.py').write_text('from collections import Counter\ndef histogram(values):return dict(Counter(values))\n')
        self.cfg.update(task='ablate', profile={'mode': 'skip'}, ablation={'stage': 'screen',
                        'variants': [{'name': 'A', 'source': str(fast), 'enabled': ['counter']}],
                        'comparisons': [{'reference': 'baseline', 'candidate': 'A'}, {'reference': 'A', 'candidate': 'baseline'}]})
        self.cfg['budget']['max_seconds'] = 300
        with patch.object(planner, 'propose', side_effect=AssertionError('Ablation cannot explore')):
            result = self.start()
            self.assertEqual(result['status'], 'ablation_complete', result)
            self.assertEqual(len(result['history']), 2)
            self.assertTrue(all(r['measured'] for r in result['history']))
            self.assertGreater(result['history'][0]['assessment']['improvement_pct'], 0)
            self.assertLess(result['history'][1]['assessment']['improvement_pct'], 0)
            self.assertEqual(controller.run_loop(self.run, resume=True), result)
        self.assertFalse((self.run / 'rounds').exists())


class AblationDesignTests(unittest.TestCase):
    def test_leave_one_out_is_full_bundle_then_each_removal(self):
        from inferrsi_loop.ablation import normalize
        plan = normalize({'design': 'leave_one_out', 'factors': ['A', 'B'], 'variants': [
            {'name': 'all', 'enabled': ['A', 'B'], 'source': '/all'},
            {'name': 'no_A', 'enabled': ['B'], 'source': '/no_A'},
            {'name': 'no_B', 'enabled': ['A'], 'source': '/no_B'}]})
        self.assertEqual(plan['comparisons'], [{'reference': 'baseline', 'candidate': 'all'},
                          {'reference': 'all', 'candidate': 'no_A', 'removed': 'A'},
                          {'reference': 'all', 'candidate': 'no_B', 'removed': 'B'}])
        self.assertEqual(normalize(plan), plan)

    def test_contribution_uses_without_factor_denominator(self):
        from inferrsi_loop.ablation import contribution
        record = {'removed': 'A', 'measured': True, 'assessment': {'metrics': {
            'baseline': 80, 'candidate': 100, 'direction': 'minimize'}}}
        row = contribution(record, {'min_gain_pct': 15})
        self.assertAlmostEqual(row['marginal_gain_pct'], 20)
        self.assertTrue(row['passes_selection_threshold'])
        row = contribution(record, {'min_gain_pct': 25})
        self.assertFalse(row['passes_selection_threshold'])
        self.assertTrue(row['measured'])


class H3PreparationTests(unittest.TestCase):
    def test_two_plus_two_and_short_requests_are_separate_from_official(self):
        import argparse
        import importlib.util
        from inferrsi_loop.common import copy_inputs
        module_spec = importlib.util.spec_from_file_location('prepare_task_fixture', ROOT / 'scripts/prepare_task.py')
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protocol = root / 'original'
            copy_inputs(ROOT / 'examples/h3/protocol', protocol, list(manifest(ROOT / 'examples/h3/protocol')))
            write(protocol / 'h3.json', {'recipe_hashes': {}, 'cases': {'starship': {'request': 'starship.json'}, 'dinner': {'request': 'dinner.json'}}})
            for case, task in [('starship', 't2va'), ('dinner', 'fl2va')]:
                write(protocol / (case + '.json'), {'extra_params': {'task': task}, 'width': 1344})
                write(root / (case + '-short.json'), {'extra_params': {'task': task}, 'width': 896})
            cfg = {'protocols': {'screen': stage(protocol), 'full': stage(protocol)}}
            args = argparse.Namespace(output=root / 'new.json', warmups=2, measurements=2, refresh_recipes=False,
                    short_starship_request=root / 'starship-short.json', short_dinner_request=root / 'dinner-short.json')
            module.protocols(cfg, args)
            for name in ('screen', 'short', 'full'):
                spec = read(Path(cfg['protocols'][name]['directory']) / 'h3.json')
                self.assertEqual((spec['warmups'], spec['measurements']), (2, 2))
                width = read(Path(cfg['protocols'][name]['directory']) / 'starship.json')['width']
                self.assertEqual(width, 896 if name == 'short' else 1344)
            self.assertEqual(read(Path(cfg['protocols']['short']['directory']) / 'h3.json')['validation_scope'], 'short_functional')
            self.assertNotIn('validation_scope', read(Path(cfg['protocols']['full']['directory']) / 'h3.json'))
            # A later task must preserve the short workload, not replace it with official requests.
            args.output = root / 'next.json'
            args.short_starship_request = args.short_dinner_request = None
            module.protocols(cfg, args)
            self.assertEqual(read(Path(cfg['protocols']['short']['directory']) / 'starship.json')['width'], 896)


class CollectorProcessTests(unittest.TestCase):
    def test_timeout_cleans_up_owned_detached_server(self):
        import os
        import signal
        from inferrsi_loop.common import run_command
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = root / 'legacy.py'
            job.write_text("import subprocess,sys,time\nfrom pathlib import Path\np=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)\nPath(sys.argv[1]).write_text(str(p.pid))\ntime.sleep(60)\n")
            launcher = root / 'launcher.py'
            protocol = ROOT / 'examples/h3/protocol'
            launcher.write_text("import sys,signal\nsys.path.insert(0,sys.argv[1])\nimport collect_profile\nsignal.signal(signal.SIGTERM,collect_profile.terminate)\ncollect_profile.script(__import__('pathlib').Path(sys.argv[2]),[sys.argv[3]])\n")
            pid_file = root / 'pid'
            result = run_command([sys.executable, str(launcher), str(protocol), str(job), str(pid_file)], root, root / 'log', 1)
            self.assertEqual(result['status'], 'timeout')
            pid = int(pid_file.read_text())
            # A killed child may briefly remain a zombie; it cannot execute work.
            status = Path('/proc') / str(pid) / 'stat'
            if status.exists():
                try:
                    self.assertEqual(status.read_text().split()[2], 'Z')
                finally:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


class ProjectScopeTests(unittest.TestCase):
    def test_whole_project_scope_includes_operator_sources_but_not_escape(self):
        self.assertTrue(planner.allowed('csrc/attention/kernel.cpp', ['.']))
        self.assertTrue(planner.allowed('models/attention.py', ['.']))
        self.assertFalse(planner.allowed('../outside.py', ['.']))
        self.assertFalse(planner.allowed('/outside.py', ['.']))

    def test_native_source_change_requires_build_contract(self):
        from inferrsi_loop import evaluator
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, value in [('base', 'before'), ('candidate', 'after')]:
                (root / name).mkdir()
                (root / name / 'kernel.cpp').write_text(value)
            with self.assertRaisesRegex(LoopError, 'rebuild-and-load'):
                evaluator.assess('screen', {}, root / 'base', root / 'candidate', 'native change',
                                 root / 'output', {}, time.time() + 100)

    def test_stale_native_binary_cannot_count_as_candidate(self):
        from inferrsi_loop.evaluator import verify_native_evidence
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'attempt/execution/measurement-001-candidate/result.json'
            write(path, {'native_build': {'source_hash': 'wrong', 'loaded_artifacts': []}})
            with self.assertRaises(LoopError):
                verify_native_evidence(root, {'path': 'attempt', 'execution': {'steps': [{'id': 'measurement-001-candidate'}]}}, {'candidate': 'correct'})
