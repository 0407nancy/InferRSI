"""Native campaign integration and the v2 orchestration boundaries."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from inferrsi_loop import ablation, controller, evaluator, knowledge, local_tests
from inferrsi_loop.common import LoopError, copy_inputs, digest, manifest, read, write
from test_loop import stage, ROOT


class V2Tests(unittest.TestCase):
    def test_same_protocol_reuses_native_campaign_and_independent_final(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); baseline=root/'baseline'; candidate=root/'candidate'
            copy_inputs(ROOT/'examples/cpu/project', baseline, ['.'])
            copy_inputs(baseline,candidate,['.'])
            evidence=root/'profile.json';write(evidence, {'fixture':True})
            diagnosis={'bottleneck':'CPU contract fixture','evidence':str(evidence)}
            cfg=stage(ROOT/'examples/cpu/protocol');deadline=time.time()+180
            (candidate/'candidate.py').write_text('from collections import Counter\ndef histogram(values): return dict(Counter(values))\n')
            first=evaluator.assess('screen',cfg,baseline,candidate,'fast',root/'one',diagnosis,deadline,
                                   campaign_root=root/'campaigns',round_limit=2,knowledge_enabled=False)
            self.assertEqual(first['verdict'],'PASS')
            again=evaluator.assess('screen',cfg,baseline,candidate,'fast',root/'one',diagnosis,deadline,
                                   campaign_root=root/'campaigns',round_limit=2,knowledge_enabled=False)
            self.assertEqual(first,again)
            (candidate/'candidate.py').write_text('def histogram(values): return {}\n')
            second=evaluator.assess('screen',cfg,baseline,candidate,'bad',root/'two',diagnosis,deadline,
                                    campaign_root=root/'campaigns',round_limit=2,knowledge_enabled=False)
            self.assertEqual(second['verdict'],'FAIL')
            self.assertEqual(first['campaign'],second['campaign'])
            native=read(Path(first['campaign'])/'state.json');self.assertEqual(len(native['rounds']),2)
            finals=evaluator.finalize_all(root/'campaigns',deadline)
            self.assertEqual(len(finals),1);self.assertEqual(finals[0]['final']['assessment']['verdict'],'PASS')
            self.assertNotEqual(finals[0]['final']['path'],first['observation'])
            self.assertEqual(evaluator.finalize_all(root/'campaigns',deadline)[0]['final'],finals[0]['final'])

    def test_off_removes_historical_inputs_but_retains_factual_roots(self):
        c={'project':{'path':'/source'},'model_assets':{'root':'/weights'},'knowledge':{'enabled':False,'factual_evidence_roots':['/metadata']},'knowledge_sources':[{'path':'/history'}],'history_files':['/past'],'reference_directory':'/mixed','knowledge_query':'secret','local_recovery':{'evidence_roots':['/']}}
        v=knowledge.prepare_policy(c)
        self.assertEqual(v['knowledge_sources'],[]);self.assertEqual(v['history_files'],[])
        self.assertIsNone(v['reference_directory']);self.assertNotIn('knowledge_query',v)
        self.assertEqual(v['local_recovery']['evidence_roots'],['/source','/weights','/metadata'])
        self.assertTrue(c['knowledge_sources'])

    def test_experience_is_idempotent_retrievable_and_not_a_false_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);write(root/'frozen/baseline/a.json',{'a':1})
            write(root/'rounds/1/proposal.json',{'hypothesis':'prefix length allocation','rationale':'减少重复传输'})
            history=[{'round':1,'stage':'screen','verdict':'INCONCLUSIVE','reason':'missing layout'}]
            c={'knowledge':{'enabled':False}}
            knowledge.writeback(c,root,history);before=manifest(root/'knowledge')
            knowledge.writeback(c,root,history);self.assertEqual(before,manifest(root/'knowledge'))
            context=knowledge.own_context(root,'prefix length')
            self.assertIn('missing layout',json.dumps(context));self.assertIn('unmeasured',json.dumps(read(root/'knowledge/catalog.json')))

    def test_task_coverage_requires_reasons_and_case_provenance(self):
        c={'workload_tasks':['t2va','fl2va']}
        contract={'cases':[{'id':'shared','provenance':['source.py:symbol'],'constraints':{'heads':28},'inputs':{'n':1024}}], 'coverage':{x:{'status':'covered','reason':'same source branch/layout','cases':['shared']} for x in c['workload_tasks']}}
        local_tests.validate_coverage(c,contract)
        del contract['coverage']['fl2va']
        with self.assertRaises(LoopError):local_tests.validate_coverage(c,contract)

    def test_automatic_ablation_builds_matrix_without_reimplementing_execution(self):
        with tempfile.TemporaryDirectory() as d:
            run=Path(d);baseline=run/'frozen/baseline';write(baseline/'a.json',{'x':1});write(baseline/'b.json',{'y':1})
            items=[]
            for n,name in enumerate(('a.json','b.json'),1):
                source=run/'rounds'/str(n)/'candidate';copy_inputs(baseline,source,['.']);write(source/name,{'new':n})
                write(source.parent/'proposal.json',{'hypothesis':name})
                items.append({'eligible_for_ablation':True,'round':n,'source':str(source),'source_hash':digest(manifest(source)),'baseline_hash':digest(manifest(baseline))})
            c={'workflow':{'after_explore':'ablate','ablation_stages':['short_t2va','short_fl2va']},'project':{'path':str(baseline),'include':['.'],'editable':['.']},'budget':{},'model':{'name':'fixture'}}
            with patch('inferrsi_loop.controller.initialize') as init,patch('inferrsi_loop.controller.run_loop',return_value={'status':'ablation_complete'}):
                result=ablation.handoff(c,run,{'candidate_optimizations':items,'overall_deadline':time.time()+300})
                self.assertEqual(result['status'],'ablation_complete')
                plan=init.call_args.args[0]['ablation']
                self.assertEqual(len(plan['comparisons']),3);self.assertEqual(len(plan['variants']),3)
                self.assertEqual(read(run/'ablation-inputs/without_candidate_001/a.json'),{'x':1})
                self.assertEqual(read(run/'ablation-inputs/without_candidate_001/b.json'),{'new':2})

    def test_native_evidence_gate_rejects_missing_cases_without_external_regrading(self):
        import subprocess,sys
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            gate=ROOT/'src/inferrsi_loop/data/evidence_gate.py'
            (root/'_evidence_gate.py').write_bytes(gate.read_bytes())
            contract={'coverage':{'t2va':{'cases':['a']}},'cases':[{'id':'a'}]}
            write(root/'result.json',{'numerical_ok':True,'local_validation':{'contract_digest':digest(contract),'weights_verified':True,'numerical_status':'pass'},'cases':[]})
            write(root/'_evidence_gate.json',{'quality_command':[sys.executable,'-c','pass'],'local_validation':contract})
            result=subprocess.run([sys.executable,str(root/'_evidence_gate.py'),'--action','quality','--candidate',d,'--baseline',d,'--result',str(root/'result.json'),'--python',sys.executable],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('Every frozen workload case',result.stderr)

    def test_malformed_weight_evidence_is_native_inconclusive(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);baseline=root/'baseline';copy_inputs(ROOT/'examples/cpu/project',baseline,['.'])
            protocol=stage(ROOT/'examples/cpu/protocol')
            protocol['quality_key']='numerical_ok'
            protocol['local_validation']={'kind':'local_module','module':'histogram'}
            write(root/'profile.json',{'fixture':True})
            result=evaluator.assess('screen',protocol,baseline,baseline,'missing proof',root/'attempt',{'bottleneck':'fixture','evidence':str(root/'profile.json')},time.time()+180)
            self.assertEqual(result['verdict'],'INCONCLUSIVE')
            self.assertEqual(result['assessment']['verdict'],'INCONCLUSIVE')
            self.assertFalse(result['evidence_verified'])
