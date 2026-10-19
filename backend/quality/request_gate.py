"""首版本地单进程共享上限；不是供应商实际配额或分布式限流。"""

import time
from collections import deque
from contextlib import contextmanager
from threading import Lock


class GateUnavailable(RuntimeError):
    pass


class RequestGate:
    def __init__(self, concurrent=2, starts=20, window=60, clock=time.monotonic):
        self.concurrent, self.starts, self.window, self.clock = concurrent, starts, window, clock
        self.lock = Lock()
        self.active = 0
        self.recent = deque()

    def try_acquire(self):
        with self.lock:
            now = self.clock()
            while self.recent and self.recent[0] <= now - self.window:
                self.recent.popleft()
            if self.active >= self.concurrent or len(self.recent) >= self.starts:
                return False
            self.active += 1
            self.recent.append(now)
            return True

    def release(self):
        with self.lock:
            self.active -= 1

    @contextmanager
    def permit(self, wait_seconds=35, control=None):
        deadline = self.clock() + wait_seconds
        while True:
            if control and control():
                raise GateUnavailable("analysis_cancelled")
            if self.try_acquire():
                break
            if self.clock() >= deadline:
                raise GateUnavailable("local_rate_limited")
            time.sleep(0.05)
        try:
            yield
        finally:
            self.release()


MODEL_REQUEST_GATE = RequestGate()
