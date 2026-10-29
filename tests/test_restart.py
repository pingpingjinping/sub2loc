from pathlib import Path
import tempfile
import unittest
from sub2loc.engine import Engine
from test_identified import config,Model,ass


class RestartTests(unittest.TestCase):
    def test_restart_resets_history_and_summaries_but_preserves_prior_runs(self):
        with tempfile.TemporaryDirectory() as root:
            engine=Engine(Path(root));files=[('01.ass',ass(['猫']))];model=Model()
            engine.translate(config(),files,client=model)
            engine.translate(config(run_nonce=''),files,client=model)
            self.assertEqual(len(model.calls),1)
            engine.translate(config(run_nonce='new-run'),files,client=model)
            self.assertEqual(len(model.calls),2)
            self.assertEqual(model.calls[-1][0]['recent_translations'],[])
            self.assertNotIn('preceding_summary',model.calls[-1][0])
            self.assertEqual(len(list(Path(root).glob('*/runs/*.json'))),2)
            engine.translate(config(run_nonce='new-run'),files,client=model)
            self.assertEqual(len(model.calls),2)
            engine.translate(config(),files,client=model)
            self.assertEqual(len(model.calls),2)
