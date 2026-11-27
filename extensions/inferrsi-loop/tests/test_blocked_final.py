import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from inferrsi_loop import evaluator
from inferrsi_loop.common import LoopError, write

class BlockedFinalTests(unittest.TestCase):
    def test_valid_blocked_status_is_not_command_failure(self):
        value={'state':'blocked','next_action':'blocked','can_finalize':False,'pending':None,'session':'/fixture/campaign','completed_rounds':1,'latest_observation':{'assessment':{'verdict':'INCONCLUSIVE'}}}
        with patch.object(evaluator,'run_command',return_value={'status':'error','returncode':2}), patch.object(evaluator,'parse_stdout',return_value=value):
            self.assertEqual(evaluator.cli(['campaign','status'],Path('/tmp/unused'),timeout=1),value)
            with self.assertRaises(LoopError): evaluator.cli(['campaign','start'],Path('/tmp/unused'),timeout=1)
        with patch.object(evaluator,'run_command',return_value={'status':'error','returncode':2}), patch.object(evaluator,'parse_stdout',return_value={'state':'blocked','error':'bad'}):
            with self.assertRaises(LoopError): evaluator.cli(['campaign','status'],Path('/tmp/unused'),timeout=1)

    def test_blocked_campaign_does_not_hide_other_final(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for name in ['a','b']:
                (root/name).mkdir();write(root/name/'session-0001-adapter.json',{'stage':{}})
            states=[{'state':'blocked','pending':None,'rounds':[]},
                    {'state':'completed','pending':None,'final':{'candidate_sha256':'verified'}}]
            with patch.object(evaluator,'status',side_effect=states):
                rows=evaluator.finalize_all(root,0)
            self.assertEqual(rows[0]['status'],'incomplete')
            self.assertEqual(rows[1]['final']['candidate_sha256'],'verified')
