"""Target policy and actual native CPU lifecycle; no H3 performance claims."""
import copy
import difflib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from inferrsi_loop import controller, objective, ssh_step
from inferrsi_loop.common import LoopError, digest, manifest, read, write
from test_loop import stage, ROOT


def fixture(root):
    source=root/'source';source.mkdir();(source/'candidate.py').write_text('import time\ndef value():\n time.sleep(0.008)\n return 7\n')
    protocol=root/'protocol';protocol.mkdir();(protocol/'instructions.txt').write_text('CPU-only orchestration fixture; fixed integer input, no model weights.')
    contract={'kind':'local_module','module':'CPU fixture','measurement_plan':{'warmups':2,'measurements':2}}
    write(protocol/'contract.json',contract)
    (protocol/'check.py').write_text('''import argparse,importlib.util,json,time,statistics,hashlib
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--project');p.add_argument('--result');p.add_argument('--action');a=p.parse_args()
s=importlib.util.spec_from_file_location('candidate',Path(a.project)/'candidate.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
c=json.loads((Path(__file__).parent/'contract.json').read_text());ok=m.value()==7
v={'functional_ok':ok,'numerical_ok':ok,'local_validation':{'contract_digest':hashlib.sha256(json.dumps(c,sort_keys=True,allow_nan=False).encode()).hexdigest(),'weights_verified':True,'numerical_status':'pass' if ok else 'fail'}}
if a.action=='measure':
 samples=[]
 for i in range(4):
  t=time.perf_counter();m.value();elapsed=(time.perf_counter()-t)*1000
  if i>=2:samples.append(elapsed)
 v.update(latency_ms=statistics.median(samples),samples=samples,warmups=2,measurements=2)
Path(a.result).write_text(json.dumps(v))
''')
    p=stage(protocol);p['quality_key']='numerical_ok';p['local_validation']=contract;p['final_reserve_seconds']=10
    protocols={'screen':p}
    for name in ['short','target']:
        for task in ['t2va','fl2va']:
            protocols[name+'_'+task]=dict(stage(protocol),task=task,final_reserve_seconds=10)
    write(root/'profile.json',{'bottleneck':'CPU delay fixture'})
    return {'project':{'path':str(source),'include':['.'],'editable':['.']},'model':{'name':'fixture','effort':'low','transport':'local_ssh'},
        'goal':'CPU lifecycle fixture','profile':{'mode':'reuse','evidence':str(root/'profile.json')},
        'budget':{'max_rounds':2,'max_seconds':600,'max_infra_retries':0},'protocols':protocols,
        'exploration':{'validation':'local_only'},'workload_tasks':['t2va','fl2va'],'human_review_required':False,
        'knowledge':{'enabled':False},'objective':{'enabled':True,'batch_candidates':1,'max_batches':2,
        'batch_seconds':120,'verification_seconds':120,'final_reserve_seconds':60,'ablation_every_batches':0,
        'targets':{t:{'screen_stage':'short_'+t,'validation_stage':'target_'+t,'workload':'fixed CPU function','min_speedup':3} for t in ['t2va','fl2va']}}}


class ObjectiveTests(unittest.TestCase):
    def test_targets_need_all_tasks_and_positive_values(self):
        with tempfile.TemporaryDirectory() as d:
            c=fixture(Path(d));objective.validate(c)
            c['objective']['targets'].pop('fl2va')
            with self.assertRaises(LoopError):objective.validate(c)

    def test_quality_and_metric_identity_cannot_be_bypassed(self):
        stage={'metric_key':'latency_ms','metric_unit':'ms','direction':'minimize'}
        d={'verdict':'PASS','evidence_verified':True,'assessment':{'quality_passed':True,
           'metrics':{'name':'latency_ms','unit':'ms','direction':'minimize','baseline':100,'candidate':70}}}
        self.assertTrue(objective.inspect_result(d,stage,{'min_speedup':1.2})['met'])
        self.assertFalse(objective.inspect_result(d,stage,{'threshold':60})['met'])
        d['assessment']['quality_passed']=False
        self.assertFalse(objective.inspect_result(d,stage,{'min_speedup':1.2})['met'])

    def test_real_two_batch_loop_reuses_native_checks_and_resumes(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);c=fixture(root);run=controller.initialize(c,root/'run')
            original=digest(manifest(run/'frozen/baseline'));baseline_paths=[]
            # Simulate only the agent's patch reply. Every test, comparison, final,
            # combination and ablation runs through the actual native CLI.
            for iteration in range(35):
                result=controller.run_loop(run,resume=True)
                self.assertNotEqual(result['status'],'blocked',result)
                if result['status']=='awaiting_local_proposal':
                    plan=ssh_step.dispatch({'action':'plan','run':str(run)})
                    child=Path(result['active_run']);number=int(child.name.split('-')[-1]);baseline_paths.append(read(child/'frozen/config.json')['project']['path'])
                    folder=child/'rounds/1';source=folder/'candidate';source.mkdir()
                    old=(child/'frozen/baseline/candidate.py').read_text()
                    new=old.replace('0.008','0.004') if number==1 else old.replace('0.004','0.0005')
                    (source/'candidate.py').write_text(new)
                    patch_text=''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),fromfile='a/candidate.py',tofile='b/candidate.py'))
                    (folder/'candidate.patch').write_text(patch_text)
                    write(folder/'proposal.json',{'hypothesis':'reduce CPU fixture delay','rationale':'fixture only',
                         'source_hash':digest(manifest(source)),'changed_files':['candidate.py']})
                if result['status'] in objective.TERMINAL:break
            self.assertEqual(result['status'],'goal_met',result)
            self.assertEqual(result['objective']['batch'],2)
            self.assertTrue(all(x['met'] for x in result['objective']['target_results'].values()))
            self.assertEqual(original,digest(manifest(run/'frozen/baseline')))
            self.assertEqual(result,controller.run_loop(run,resume=True))
            self.assertTrue((run/'ablations/batch-002/summary.json').exists())
            self.assertIn('0.004',(run/'batches/batch-002/frozen/baseline/candidate.py').read_text())
            self.assertTrue(result['objective']['factors'])
            self.assertTrue(all('/confirm/' in x['decision'] for x in result['objective']['target_results'].values()))

    def test_exhausted_budget_never_reports_success(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);c=fixture(root);run=controller.initialize(c,root/'run');s=read(run/'state.json');s['deadline']=time.time()-1;write(run/'state.json',s)
            self.assertEqual(controller.run_loop(run,resume=True)['status'],'goal_not_met')

    def test_pending_child_handoff_cannot_escape_parent(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);c=fixture(root);run=controller.initialize(c,root/'run');s=read(run/'state.json');s['objective']=dict(objective.initial(run),active_run='/tmp/unrelated');write(run/'state.json',s)
            with self.assertRaises(LoopError):ssh_step.dispatch({'run':str(run),'action':'plan'})
