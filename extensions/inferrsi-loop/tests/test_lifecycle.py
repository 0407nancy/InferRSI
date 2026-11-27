"""Exercise recovery transitions with real relocated CPU protocol execution."""
import base64
from pathlib import Path
import sys
import tempfile
import time
import unittest

from inferrsi_loop import lifecycle, local_tests
from inferrsi_loop.common import digest, manifest, read, sha, write


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root / 'run'
        self.folder = self.run / 'rounds/1'
        self.folder.mkdir(parents=True)
        baseline = self.run / 'frozen/baseline'
        baseline.mkdir(parents=True)
        (baseline/'module.py').write_text('value=1')
        candidate = self.folder/'candidate'
        candidate.mkdir()
        (candidate/'module.py').write_text('value=2')
        write(self.run/'state.json', {'deadline': time.time()+30})
        self.cfg = {'project':{'editable':['.']}, 'local_recovery':{'enabled':True,'max_protocol_versions':3,
            'max_evidence_rounds':2,'evidence_roots':[str(self.root/'allowed')]},
            'protocols':{'screen':{'resources':{'count':0},'min_improvement_pct':1,
                                 'repetitions':1,'warmups':0,'timeout_seconds':10}}}
        self.request = {'source_hash':digest(manifest(candidate)),'changed_files':['module.py']}
        write(self.folder/'local-test-request.json', self.request)

    def tearDown(self):
        self.temp.cleanup()

    def upload(self, broken=False):
        spec = {'paths':['module.py'], 'scope':'cpu fixture','metric_key':'latency','metric_unit':'ms','direction':'minimize',
                'measure_command':['{python}','{protocol}/check.py','{result}'],
                'quality_command':['{python}','{protocol}/check.py','{result}'],
                'local_validation':{'kind':'local_module','module':'fixture','exercised_symbols':['value'],
                    'weight_origin':'parameter_free','evidence_files':[{'path':'{protocol}/input.json','role':'input'}],
                    'numerical_rule':{'mode':'exact'}}}
        script = '''import json,hashlib,sys
from pathlib import Path
p=Path(__file__).parent
c=json.loads((p/'adapter.json').read_text())['local_validation']
e=c['evidence_files'][0]
assert hashlib.sha256((p/'input.json').read_bytes()).hexdigest()==e['sha256']
'''
        if broken:
            script += "assert Path(e['path']) == p/'input.json', 'original relocation bug'\n"
        script += "Path(sys.argv[1]).write_text(json.dumps({'numerical_ok':True,'local_validation':{'numerical_status':'pass','weights_verified':True,'contract_digest':hashlib.sha256(json.dumps(c,sort_keys=True,allow_nan=False).encode()).hexdigest()}}))\n"
        import json
        files = {'adapter.json':json.dumps(spec),'instructions.txt':'CPU test only','input.json':'{"fixed":1}','check.py':script}
        return local_tests.register(self.cfg,self.run,self.folder,{'request_digest':digest(self.request),
            'files':{n:base64.b64encode(v.encode()).decode() for n,v in files.items()}})

    def test_failed_preflight_retains_original_and_repairs_in_new_version(self):
        first = self.upload(broken=True)
        old_hash = digest(manifest(first['protocol']['directory']))
        lifecycle.preflight(self.cfg,self.run,self.folder)
        self.assertEqual(lifecycle.cursor(self.folder)['status'],'generation')
        self.assertEqual(lifecycle.cursor(self.folder)['version'],2)
        self.assertFalse((self.folder/'local-test-receipt.json').exists())
        self.assertIn('original relocation bug',str(lifecycle.cursor(self.folder)['repairs']))
        second = self.upload()
        lifecycle.preflight(self.cfg,self.run,self.folder)
        self.assertEqual(lifecycle.cursor(self.folder)['status'],'ready')
        self.assertEqual(read(self.folder/'local-test-receipt.json')['protocol_version'],2)
        self.assertEqual(digest(manifest(first['protocol']['directory'])),old_hash)
        self.assertNotEqual(first['protocol']['directory'],second['protocol']['directory'])
        self.assertEqual(second['protocol']['min_improvement_pct'],1)

    def test_post_freeze_failure_creates_distinct_version_without_editing_old(self):
        first=self.upload()
        lifecycle.preflight(self.cfg,self.run,self.folder)
        self.assertTrue(lifecycle.revise(self.cfg,self.folder,'evaluation_protocol_failure',{'error':'missing measurement field'}))
        self.assertEqual(lifecycle.cursor(self.folder)['version'],2)
        self.assertTrue(Path(first['protocol']['directory']).is_dir())
        second=self.upload()
        lifecycle.preflight(self.cfg,self.run,self.folder)
        self.assertEqual(read(self.folder/'local-test-receipt.json')['protocol_version'],2)

    def test_repeated_failure_exhausts_without_blind_retries(self):
        self.assertTrue(lifecycle.revise(self.cfg,self.folder,'same',{'error':'unchanged'}))
        self.assertFalse(lifecycle.revise(self.cfg,self.folder,'same',{'error':'unchanged'}))
        self.assertEqual(lifecycle.cursor(self.folder)['status'],'exhausted')

    def test_evidence_allowlist_and_dedup(self):
        allowed=self.root/'allowed';allowed.mkdir();(allowed/'source.py').write_text('known=1')
        forbidden=self.root/'secret.txt';forbidden.write_text('never transport')
        payload={'request_digest':digest(self.request),'missing_evidence':[
            {'kind':'text','path':str(allowed/'source.py'),'reason':'source'},
            {'kind':'text','path':str(forbidden),'reason':'forbidden'}]}
        receipt=lifecycle.supplement(self.cfg,self.run,self.folder,payload)
        self.assertEqual([x['status'] for x in receipt['files']],['read','unavailable'])
        self.assertNotIn('never transport',str(receipt))
        self.assertTrue(lifecycle.supplement(self.cfg,self.run,self.folder,payload)['repeated'])
        self.assertEqual(len(list((self.folder/'supplements').glob('*/receipt.json'))),1)

    def test_log_evidence_is_bounded_and_retains_provenance(self):
        allowed = self.root/'allowed'
        allowed.mkdir()
        limit = 2*1024*1024
        (allowed/'server.log').write_text('local_heads=8\npacked_total=128\n')
        (allowed/'boundary.log').write_bytes(b'x'*limit)
        (allowed/'large.log').write_bytes(b'x'*(limit+1))
        (allowed/'weights.bin').write_bytes(b'not text evidence')
        (allowed/'invalid.log').write_bytes(b'\xff')
        outside = self.root/'outside.log'
        outside.write_text('must not read')
        (allowed/'escape.log').symlink_to(outside)
        (allowed/'directory.log').mkdir()
        names = ['server.log', 'boundary.log', 'large.log', 'weights.bin',
                 'invalid.log', 'escape.log', 'directory.log']
        payload = {'request_digest': digest(self.request), 'missing_evidence': [
            {'kind': 'text', 'path': str(allowed/name), 'reason': 'inspect log'} for name in names]}
        receipt = lifecycle.supplement(self.cfg, self.run, self.folder, payload)
        self.assertEqual([row['status'] for row in receipt['files']],
                         ['read', 'read'] + ['unavailable']*5)
        for row in receipt['files'][:2]:
            saved = self.folder/'supplements/01'/row['file']
            self.assertEqual(saved.read_bytes(), Path(row['path']).read_bytes())
            self.assertEqual(row['content_sha256'], sha(saved))
        self.assertIn('exceeds 2 MiB', receipt['files'][2]['reason'])
        self.assertIn('Outside declared evidence roots', receipt['files'][5]['reason'])
        self.assertTrue(lifecycle.supplement(self.cfg, self.run, self.folder, payload)['repeated'])

    def test_repair_bundle_preserves_large_fixed_input_bytes(self):
        protocol=self.folder/'protocol-versions/v01/protocol'
        protocol.mkdir(parents=True)
        content=b'["'+b'x'*(2*1024*1024+32)+b'"]'
        (protocol/'inputs.json').write_bytes(content)
        (protocol/'weights.safetensors').write_bytes(b'never transfer weights')
        write(self.folder/'context.json',{})
        reference=self.root/'reference';reference.mkdir()
        self.cfg['protocols']['screen']['directory']=str(reference)
        self.cfg['model']={'name':'test'}
        files=local_tests.bundle(self.cfg,self.run,self.folder)['files']
        key='previous_protocols/v01/protocol/inputs.json'
        self.assertEqual(base64.b64decode(files[key]),content)
        self.assertFalse(any(n.endswith('.safetensors') for n in files))

if __name__ == '__main__':
    unittest.main()
