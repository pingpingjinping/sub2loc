import unittest
from sub2loc.subtitles import render_srt
from sub2loc.framing import CueFrame
from sub2loc.timing import ThroughputETA

class OverlapTests(unittest.TestCase):
    def test_partial_overlap_example(self):
        text=render_srt([(10000,20000,'정말크다!!'),(10000,15000,'진짜루'),(15000,30000,'허거덩스')])
        self.assertEqual(text,'1\r\n00:00:10,000 --> 00:00:15,000\r\n정말크다!!\r\n진짜루\r\n\r\n2\r\n00:00:15,000 --> 00:00:20,000\r\n정말크다!!\r\n허거덩스\r\n\r\n3\r\n00:00:20,000 --> 00:00:30,000\r\n허거덩스\r\n\r\n')

    def test_tail_repair_is_narrow_and_restore_remains_strict(self):
        frame=CueFrame('','','', ['original'])
        self.assertEqual(frame.repair_trailing_token('앞⟪S2L:0⟫뒤⟪S2L:1⟫'),('앞⟪S2L:0⟫뒤',True))
        for text in ('앞⟪S2L:1⟫','앞⟪S2L:0⟫⟪S2L:0⟫','⟪S2L:1⟫앞⟪S2L:0⟫','앞⟪S2L:0⟫⟪S2L:2⟫'):
            self.assertEqual(frame.repair_trailing_token(text),(text,False))
        with self.assertRaises(ValueError):frame.restore('앞⟪S2L:0⟫뒤⟪S2L:1⟫')

    def test_eta_excludes_cached_work_and_does_not_inflate_inflight(self):
        now=[100.0];eta=ThroughputETA(lambda:now[0])
        eta.update('preflight',90,110);eta.update('request',90,110)
        now[0]=110;self.assertEqual(eta.update('validated',100,110)['eta_seconds'],10)
        eta.update('request',100,110);now[0]=115
        self.assertEqual(eta.update('stream',100,110)['eta_seconds'],5)
        now[0]=125;self.assertTrue(eta.update('stream',100,110)['eta_overdue'])
        self.assertEqual(eta.update('validated',110,110)['eta_seconds'],0)
