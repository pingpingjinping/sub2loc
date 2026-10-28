import os
from pathlib import Path
import tempfile
import unittest

from sub2loc.timing import RunClock, format_duration


class RunTimingTests(unittest.TestCase):
    def test_minutes_hours_and_subsecond_format(self):
        self.assertEqual(format_duration(0.9), '0초')
        self.assertEqual(format_duration(923.7), '15분 23초')
        self.assertEqual(format_duration(3661), '1시간 1분 1초')

    def test_finish_is_stable_and_new_invocation_starts_at_zero(self):
        now = [10.0]; clock = RunClock(lambda: now[0])
        self.assertEqual(clock.elapsed(), 0)
        clock.start(); now[0] = 933.7
        self.assertAlmostEqual(clock.elapsed(), 923.7)
        self.assertAlmostEqual(clock.finish(), 923.7)
        now[0] = 1000
        self.assertAlmostEqual(clock.finish(), 923.7)
        clock.start(); self.assertEqual(clock.elapsed(), 0)
        now[0] += 3; self.assertEqual(clock.finish(), 3)


os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtWidgets import QApplication
    from sub2loc.desktop import MainWindow
    HAS_QT = True
except ImportError:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, 'Install desktop requirements')
class RunTimingUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def test_progress_does_not_reset_elapsed_and_completion_remains_frozen(self):
        with tempfile.TemporaryDirectory() as folder:
            window = MainWindow(Path(folder)); now = [100.0]
            window.run_clock = RunClock(lambda: now[0]); window.run_clock.start()
            window.elapsed_label.setVisible(True); window.elapsed_timer.start()
            window.job_progress(dict(phase='request', message='대사 요청', done=0, total=2))
            now[0] = 160
            window.job_progress(dict(phase='validated', message='묶음 저장', done=1, total=2))
            window.show_elapsed_time()
            self.assertEqual(window.elapsed_label.text(), '전체 경과 1분 0초')
            self.assertTrue(window.elapsed_timer.isActive())
            now[0] = 1023.7; window.job_complete(['result.ass'])
            self.assertIn('자막 1개 · 총 15분 23초', window.status.text())
            self.assertFalse(window.elapsed_timer.isActive())
            now[0] = 2000; window.finish_run_time()
            self.assertEqual(window.elapsed_label.text(), '총 소요 15분 23초')
            window.close()

    def test_stop_keeps_clock_running_until_worker_finishes_with_error(self):
        class Worker:
            def __init__(self):
                import threading
                self.cancel = threading.Event()
        with tempfile.TemporaryDirectory() as folder:
            window = MainWindow(Path(folder)); now = [10.0]
            window.run_clock = RunClock(lambda: now[0]); window.run_clock.start()
            window.elapsed_timer.start(); window.worker = Worker()
            now[0] = 25; window.stop()
            self.assertTrue(window.elapsed_timer.isActive())
            now[0] = 42; window.job_error('중단했습니다.')
            self.assertIn('경과 32초', window.status.text())
            self.assertFalse(window.elapsed_timer.isActive())
            window.worker = None; window.close()

    def test_split_reason_is_retained_after_next_request_and_credentials_are_redacted(self):
        with tempfile.TemporaryDirectory() as folder:
            window = MainWindow(Path(folder)); window.key.setText('TEST-CREDENTIAL')
            window.job_progress(dict(phase='request', message='40–46 대사 요청'))
            window.job_progress(dict(phase='retry', message='ASS 태그 불일치 · 묶음을 더 작게 나눕니다'))
            window.job_progress(dict(phase='request', message='40–40 대사 요청'))
            log = window.run_log.toPlainText()
            self.assertIn('40–46', log); self.assertIn('ASS 태그 불일치', log); self.assertIn('40–40', log)
            window.log_run_event('키 TEST-CREDENTIAL')
            self.assertNotIn('TEST-CREDENTIAL', window.run_log.toPlainText())
            window.wait_timer.stop(); window.close()


if __name__ == '__main__': unittest.main()
