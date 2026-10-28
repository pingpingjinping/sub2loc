"""Monotonic elapsed time for one translation invocation."""
import time


def format_duration(seconds):
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f'{hours}시간 {minutes}분 {seconds}초'
    if minutes:
        return f'{minutes}분 {seconds}초'
    return f'{seconds}초'


class RunClock:
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.started = None
        self.duration = 0.0

    def start(self):
        self.duration = 0.0
        self.started = self.clock()

    def elapsed(self):
        return max(0.0, self.clock() - self.started) if self.started is not None else self.duration

    def finish(self):
        self.duration = self.elapsed()
        self.started = None
        return self.duration
