import json,os,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from inferrsi_loop import model_selection,adapter
from inferrsi_loop.common import LoopError,read

class ModelSelectionTests(unittest.TestCase):
 def test_latest_current_turn_overrides_global_defaults(self):
  with tempfile.TemporaryDirectory() as d:
   home=Path(d);rollout=home/'current.jsonl'
   rollout.write_text('\n'.join(json.dumps({'type':'turn_context','payload':v}) for v in [
    {'model':'old','effort':'low'},{'model':'chosen','effort':'high','turn_id':'turn2'}])+'\n{"unfinished":')
   (home/'config.toml').write_text('model="wrong-default"\nmodel_reasoning_effort="low"')
   with sqlite3.connect(home/'state_5.sqlite') as c:
    c.execute('create table threads(id text, rollout_path text)');c.execute('insert into threads values (?,?)',('current',str(rollout)))
   with patch.dict(os.environ,{'CODEX_HOME':d,'CODEX_THREAD_ID':'current'}):
    session=model_selection.session_context();self.assertEqual(session['model'],'chosen');self.assertEqual(session['effort'],'high')
    resolved=model_selection.retain(home/'run',{}, {},session)
    with patch.object(adapter.platform,'system',return_value='Darwin'),patch.object(adapter.shutil,'which',return_value='/bin/codex'):
     argv=adapter.command({'name':resolved['model'],'effort':resolved['effort']},home,home/'schema',home/'answer')
    self.assertIn('chosen',argv);self.assertIn('model_reasoning_effort="high"',argv)
    self.assertNotIn('model_provider',str(argv));self.assertNotIn('base_url',str(argv))
    self.assertEqual(model_selection.retain(home/'run',{}, {},{'model':'later','effort':'low'}),resolved)
    with self.assertRaises(LoopError):model_selection.retain(home/'run',{'model':'changed'},{},session)
 def test_partial_override_inherits_other_field(self):
  s={'model':'session','effort':'high'}
  self.assertEqual(model_selection.resolve({'model':'override'},{},s)['effort'],'high')
  self.assertEqual(model_selection.resolve({}, {'name':'task'},s)['model'],'task')
  self.assertEqual(model_selection.resolve({'model':'launch'},{'name':'task'},s)['model'],'launch')
 def test_no_session_no_silent_default(self):
  with self.assertRaises(LoopError):model_selection.resolve({}, {}, None)
  self.assertEqual(model_selection.resolve({'model':'explicit','effort':'low'}, {},None)['model'],'explicit')
 def test_environment_preserves_local_routing(self):
  with patch.dict(os.environ,{'CODEX_HOME':'/tmp/custom-home','OPENAI_BASE_URL':'http://localhost/test'}):
   env=adapter.model_environment({});self.assertEqual(env['CODEX_HOME'],'/tmp/custom-home');self.assertEqual(env['OPENAI_BASE_URL'],'http://localhost/test')
 def test_remote_freezes_resolved_selection_without_modifying_template(self):
  from inferrsi_loop import ssh_step
  from inferrsi_loop.common import write
  from test_loop import ROOT,stage
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);cfg={'project':{'path':str(ROOT/'examples/cpu/project'),'include':['candidate.py'],'editable':['.']},'model':{'transport':'local_ssh'},'goal':'fixture','profile':{'mode':'reuse','evidence':str(root/'profile.json')},'budget':{'max_rounds':1,'max_seconds':60},'protocols':{'screen':stage(ROOT/'examples/cpu/protocol')}}
   write(root/'profile.json',{'latency_ms':1})
   write(root/'task.json',cfg)
   req={'run':str(root/'run'),'config':str(root/'task.json')}
   self.assertNotIn('name',ssh_step.dispatch(dict(req,action='configuration'))['model'])
   ssh_step.dispatch(dict(req,action='initialize',model_selection={'name':'session-model','effort':'high'}))
   self.assertEqual(read(root/'run/frozen/config.json')['model']['name'],'session-model')
   self.assertEqual(ssh_step.dispatch(dict(req,action='configuration'))['model']['effort'],'high')
   self.assertNotIn('name',read(root/'task.json')['model'])
