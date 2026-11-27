import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from inferrsi_loop import knowledge, controller, runner, local_tests
from inferrsi_loop.common import LoopError, read, write
from inferrsi_loop.configuration import filled, launch

ROOT = Path(__file__).resolve().parents[1]

def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ConfigurationKnowledgeTests(unittest.TestCase):
    def test_unfilled_templates_fail_before_execution(self):
        with self.assertRaisesRegex(LoopError, 'template field'):
            filled(read(ROOT/'config/task.template.json'))
        with self.assertRaisesRegex(LoopError, 'template field'):
            launch(ROOT/'config/launch.template.json')
        # A published filled example must also satisfy the exact same check.
        for path in (ROOT/'config/examples').glob('synthetic.*.json'):
            filled(read(path))

    def test_first_use_has_no_dependency_on_personal_h3_config_or_captures(self):
        mod = load('prepare_task')
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            repo = root/'repo'; repo.mkdir(); (repo/'operator.py').write_text('value = 1\n')
            refs = root/'refs'; refs.mkdir(); (refs/'guide.txt').write_text('Test references')
            write(root/'profile.json', {'bottleneck':'operator'})
            cfg = read(ROOT/'config/task.template.json')
            cfg.update(goal='test', model={'name':'test','effort':'medium','transport':'local_ssh'},
                       reference_directory=str(refs), knowledge_query='operator', knowledge_sources=[],
                       runtime={'inference_python':sys.executable}, model_assets={'root':str(root)},
                       local_recovery={'enabled':True,'evidence_roots':[str(repo)]},sync={'automatic':False})
            cfg['project']['path']=str(repo)
            cfg['profile']['evidence']=str(root/'profile.json')
            stage=cfg['protocols']['screen']
            stage.update(directory=str(ROOT/'examples/local/protocol'), instructions=str(ROOT/'examples/local/protocol/instructions.txt'),resources={'count':0})
            write(root/'base.json',cfg)
            args=argparse.Namespace(base=root/'base.json',output=root/'task.json',task='explore',goal=None,
                model=None,effort=None,project=None,editable=None,rounds=3,seconds=3600,history=[],
                short_starship_request=None,short_dinner_request=None,warmups=2,measurements=2,
                refresh_recipes=False,profile='reuse',evidence=None,local_protocols=None,
                local_min_gain_pct=5,ablation_plan=None)
            mod.prepare(args)
            actual=read(root/'task.json')
            self.assertEqual(actual['measurement_plan'],{'warmups':2,'measurements':2})
            self.assertEqual(actual['screen_routes'],[])
            self.assertEqual(actual['protocols']['screen']['min_improvement_pct'],5)
            self.assertTrue((root/'task-protocol/local_validation.py').is_file())
            run=controller.initialize(actual,root/'run')
            state=controller.run_loop(run)
            self.assertEqual(state['status'],'awaiting_local_proposal')
            self.assertFalse((run/'rounds/1/evaluation').exists())
            # A new user's existing collector uses the same frozen Runner;
            # no personal H3 profiler directory is needed.
            collector=root/'collector'; collector.mkdir()
            (collector/'collect.py').write_text('print("collector fixture")\n')
            cfg['profile'].update(directory=str(collector),argv=[sys.executable,'{profile}/collect.py'],
                                  resources={'count':0},timeout_seconds=30)
            cfg['knowledge_sources']=[{'id':'refs','kind':'directory','path':str(refs)}]
            write(root/'base.json',cfg)
            args.output=root/'adaptive.json';args.profile='adaptive';args.max_refreshes=1
            mod.prepare(args)
            self.assertEqual(read(args.output)['profile']['directory'],str(collector))
            self.assertEqual(read(args.output)['profile']['max_refreshes'],1)
            self.assertIn(str(refs),read(args.output)['local_recovery']['evidence_roots'])

    def test_index_retains_failed_and_successful_sources_without_reclassifying(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); source=root/'history'
            write(source/'manual/norm-failed.json', {'verdict':'FAIL','hypothesis':'fused norm'})
            write(source/'loop/results.json', {'status':'blocked','reason':'missing checkpoint'})
            write(source/'loop/frozen/source.json', {'ignore':'duplicate source'})
            snapshot={'source':{'url':'https://example/wiki/manual'},'response':{'ok':True,'data':{'document':{
                'revision_id':5,'document_id':'doc','content':'<h2 id="one">旧容器</h2><p>Swiglu failed</p><h2 id="two">新容器</h2><p>prefix attention candidate</p>'}}}}
            sources=[{'id':'manual','kind':'lark','url':'https://example/wiki/manual','environment':'mixed'},
                     {'id':'files','kind':'directory','path':str(source),'environment':'historical'}]
            knowledge.freeze_project(sources,root/'index',{'manual':snapshot})
            entries=read(root/'index/catalog.json')['entries']
            self.assertEqual(len(entries),4)
            self.assertEqual({x.get('declared_outcome') for x in entries if x['source_id']=='files'}, {'FAIL','blocked'})
            self.assertTrue(any(x['origin'].endswith('#two') and x['revision']==5 for x in entries))
            self.assertEqual(read(source/'manual/norm-failed.json')['verdict'],'FAIL')
            try:
                import inferrsi
            except ImportError:
                return
            result=knowledge.project_context(root/'index','fused norm')
            self.assertEqual(result['ranker'],'inferrsi.knowledge.knowledge_find')
            self.assertIn('fused norm',result['items'][0]['excerpt'])

    def test_missing_large_and_unread_sources_are_not_silent(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); (root/'large.log').write_text('x'*20)
            sources=[{'id':'small','kind':'directory','path':str(root),'max_file_bytes':10},
                     {'id':'absent','kind':'directory','path':str(root/'absent'),'required':False}]
            knowledge.freeze_project(sources,root/'index')
            coverage=read(root/'index/coverage.json')
            self.assertFalse(coverage['complete_for_declared_text'])
            self.assertEqual(coverage['sources'][0]['omissions'][0]['reason'],'large_text')
            self.assertEqual(coverage['sources'][1]['omissions'][0]['reason'],'missing_directory')

    def test_missing_required_document_cannot_be_treated_as_no_history(self):
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaisesRegex(LoopError,'Missing local document'):
                knowledge.freeze_project([{'id':'manual','kind':'lark','url':'https://example/wiki/x'}],Path(name)/'index')

    def test_large_early_log_does_not_starve_later_experiment(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); (root/'early').mkdir()
            (root/'early/verbose.log').write_text('x'*90)
            write(root/'later/result.json',{'status':'failed'})
            sources=[{'id':x,'kind':'directory','path':str(root/x)} for x in ('early','later')]
            knowledge.freeze_project(sources,root/'index',max_bytes=100)
            catalog=read(root/'index/catalog.json')
            self.assertTrue(any(e['origin'].endswith('later/result.json') for e in catalog['entries']))
            self.assertTrue(any(e['path'].endswith('verbose.log') for e in catalog['unindexed']))

    def test_local_test_generation_receives_full_history_not_only_excerpts(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);folder=root/'rounds/1'
            write(folder/'context.json',{'project_knowledge':{'items':[]}})
            write(folder/'local-test-request.json',{'round':1})
            write(root/'state.json',{'deadline':1})
            for p in (root/'frozen/baseline',folder/'candidate',root/'protocol'):
                p.mkdir(parents=True);(p/'fixture.py').write_text('x=1\n')
            write(root/'knowledge/catalog.json',{'entries':[{'origin':'older-failure'}]})
            write(root/'knowledge/documents/raw.json',{'unneeded':'large API envelope'})
            cfg={'model':{},'protocols':{'screen':{'directory':str(root/'protocol')}},
                 'knowledge_directory':str(root/'knowledge')}
            bundle=local_tests.bundle(cfg,root,folder)
            self.assertIn('references/project-knowledge/catalog.json',bundle['files'])
            self.assertNotIn('references/project-knowledge/documents/raw.json',bundle['files'])

    def test_failed_lark_envelope_does_not_become_empty_success(self):
        response=subprocess.CompletedProcess([],0,json.dumps({'ok':False}), '')
        with patch.object(knowledge.subprocess,'run',return_value=response):
            with self.assertRaises(LoopError):
                knowledge.fetch_documents([{'id':'manual','kind':'lark','url':'https://example/wiki/x'}])

    def test_allowlist_does_not_select_other_idle_cards(self):
        rows=[{'id':i,'memory_used_mb':1 if i<2 else 0,'utilization_pct':0} for i in range(8)]
        with tempfile.TemporaryDirectory() as name, patch.object(runner,'probe',return_value=rows):
            with self.assertRaises(LoopError):
                with runner.resources({'count':2,'allow':[0,1],'lock_dir':name},Path(name)):
                    self.fail('must not select 2-7')

    def test_launcher_routes_driver_and_reporter_to_user_config(self):
        mod=load('run_h3')
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            cfg=read(ROOT/'config/examples/synthetic.launch.json')
            cfg.update(ssh='/tools/my-ssh',host='other',expected_user='alice',container='my-container',
                remote_python='/env/python',remote_package='/code/loop',remote_runs='/runs',local_runs=str(root/'local'),
                lark='/tools/lark',document='https://example/wiki/results',codex='/tools/codex',model='my-model')
            write(root/'launch.json',cfg)
            report={'status':'screen_only_complete','summary':{},'accepted':[],'validated_pending_review':[]}
            calls=[]
            def fake(argv,**kw):
                calls.append(argv)
                return subprocess.CompletedProcess(argv,0,json.dumps(report),'')
            with patch.object(sys,'argv',['run_h3','--run-id','fresh','--remote-config','/new/task.json',
                '--launch-config',str(root/'launch.json'),'--no-sync']), patch.object(mod.subprocess,'run',side_effect=fake), patch.object(mod.reporter,'enqueue'):
                self.assertEqual(mod.main(),0)
            self.assertIn('alice',calls[0]);self.assertIn('my-container',calls[0])
            self.assertIn('/code/loop',calls[0])
            self.assertEqual(calls[1][:3],['/tools/my-ssh','-T','other'])
            self.assertIn('/code/loop/scripts/report_results.py',calls[1][-1])
            self.assertTrue((root/'local/fresh/results.json').is_file())
