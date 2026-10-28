"""Monotonic elapsed time for one translation invocation."""
import time
from collections import deque


class ThroughputETA:
    """Estimate from completed request work, excluding cache and setup time.

    A request with no accepted rows stays open so its retry cost is included
    when progress finally arrives. In-flight time consumes the prediction; it
    never increases the measured seconds per cue. Overdue predictions wait for
    a completed sample rather than showing zero while work is still pending.
    """
    def __init__(self, clock=None, window=8):
        self.clock = clock or time.monotonic
        self.samples = deque(maxlen=window)
        self.request_started = None
        self.request_done = 0

    def update(self, phase, done, total):
        now = self.clock()
        if phase == 'request' and self.request_started is None:
            self.request_started, self.request_done = now, done
        if phase == 'validated' and self.request_started is not None and done > self.request_done:
            duration = max(0.0, now - self.request_started)
            if duration:
                self.samples.append((duration, done - self.request_done))
            self.request_started = None
        if done >= total:
            return dict(eta_seconds=0.0, eta_overdue=False)
        if not self.samples:
            return dict(eta_seconds=None, eta_overdue=False)
        seconds = sum(s for s, _ in self.samples) / sum(n for _, n in self.samples) * (total - done)
        if self.request_started is not None:
            seconds -= max(0.0, now - self.request_started)
        return dict(eta_seconds=seconds if seconds > 0 else None, eta_overdue=seconds <= 0)


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
