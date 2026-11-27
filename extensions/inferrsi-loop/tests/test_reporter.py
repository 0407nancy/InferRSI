import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from inferrsi_loop import reporter
from inferrsi_loop.common import write

class ReporterTests(unittest.TestCase):
    def payload(self):
        return {'run_id':'example-01','status':'complete','report':{'started_at':0,'run':'/runs/example-01','summary':{'candidate_count':2,'effective_experiments':1},'other_candidates':[{'round':1,'hypothesis':'Reduce copies < safely','status':'candidate_optimization','patch':'/runs/example-01/rounds/1/candidate.patch','decisions':{'screen':{'assessment':{'metrics':{'baseline':10,'candidate':9,'unit':'ms'},'improvement_pct':10,'quality_passed':True}}}},{'round':2,'hypothesis':'Missing fixture','status':'unmeasured_missing_evidence_or_protocol','decisions':{}}]}}
    def test_scope_missing_measurement_and_xml_escaping(self):
        payload=self.payload()
        payload['report']['other_candidates'][0]['rationale']='减少中间拷贝可能降低内存访问开销。'
        text=reporter.render(payload);root=ET.fromstring('<fragment>'+text+'</fragment>')
        self.assertEqual(len(root.findall('h2')),1)
        self.assertEqual(len(root.findall('.//tbody/tr')),2)
        self.assertIn('未测量',text);self.assertIn('10.00%',text)
        self.assertIn('Reduce copies &lt; safely',text)
        self.assertIn('/runs/example-01/results.json',text)
        self.assertIn('不代表真实请求',text)
        self.assertIn('尝试原因：第1项：减少中间拷贝可能降低内存访问开销。',text)
    def test_legacy_migration_idempotence_and_bounded_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp);payload=self.payload();write(run/'sync-outbox.json',payload)
            current='<fragment><p id="old">InferRSI-loop:example-01 | old</p></fragment>'
            updates=[]
            def cli(argv,**kw):
                nonlocal current
                if '+update' in argv:
                    updates.append(argv)
                    root=ET.fromstring('<fragment>'+(run/'sync-payload.xml').read_text()+'</fragment>')
                    for n,node in enumerate(root.iter()):node.set('id',str(n))
                    current=ET.tostring(root,encoding='unicode')
                content='<fragment><outline><h2 id="title">Old example-01</h2></outline></fragment>' if 'outline' in argv else current
                return SimpleNamespace(returncode=0,stdout=json.dumps({'ok':True,'data':{'document':{'content':content,'revision_id':1}}}))
            with patch('shutil.which',return_value='/bin/lark'),patch('subprocess.run',side_effect=cli):
                reporter.sync(run,'doc');reporter.sync(run,'doc')
                self.assertEqual(len(updates),1)
                self.assertIn('--start-block-id',updates[0]);self.assertIn('old',updates[0])
                payload['status']='updated';write(run/'sync-outbox.json',payload)
                reporter.sync(run,'doc')
                self.assertEqual(len(updates),2)
                self.assertIn('--end-block-id',updates[1])
                # Generated reasons can follow new proposals before any manual edit.
                payload['report']['other_candidates'][0]['rationale']='New measured bottleneck hypothesis'
                write(run/'sync-outbox.json',payload);reporter.sync(run,'doc')
                self.assertIn('New measured bottleneck hypothesis',current)
                # Simulate a manually supplemented paragraph and a legacy receipt.
                root=ET.fromstring(current)
                for node in root.findall('p'):
                    if (node.text or '').startswith('尝试原因：'):
                        node.text='尝试原因：手工补充的具体依据。'
                current=ET.tostring(root,encoding='unicode')
                write(run/'sync-receipt.json',{'format':2,'digest':'old'})
                reporter.sync(run,'doc')
                self.assertIn('手工补充的具体依据。',(run/'report-notes.json').read_text())
                # Report-only collection replaces results, but must not lose the note.
                payload['status']='collected_again'
                write(run/'summary.json',{'run_id':'example-01','status':payload['status']})
                write(run/'results.json',payload['report'])
                rebuilt=reporter.enqueue(run)
                self.assertEqual(rebuilt['report']['report_notes']['attempt_reason'],'手工补充的具体依据。')
                reporter.sync(run,'doc');count=len(updates);reporter.sync(run,'doc')
                self.assertEqual(len(updates),count)
                root=ET.fromstring(current)
                reasons=[n.text for n in root.findall('p') if (n.text or '').startswith('尝试原因：')]
                self.assertEqual(reasons,['尝试原因：手工补充的具体依据。'])

    def test_collection_transports_existing_planner_rationale(self):
        import importlib.util
        script=Path(__file__).resolve().parents[1]/'scripts/report_results.py'
        spec=importlib.util.spec_from_file_location('report_results_test',script)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp)
            write(run/'summary.json',{'run_id':run.name,'status':'screen_only_complete','scope':'local','model':'test'})
            write(run/'frozen/config.json',{'task':'explore'})
            write(run/'state.json',{'started_at':0})
            write(run/'rounds/1/proposal.json',{'hypothesis':'reduce copies','rationale':'减少重复访存。','source_hash':'fixture'})
            result=module.collect(run)
            self.assertEqual(result['other_candidates'][0]['rationale'],'减少重复访存。')
            reporter.enqueue(run)
            self.assertIn('尝试原因：第1项：减少重复访存。',reporter.render(json.loads((run/'sync-outbox.json').read_text())))
