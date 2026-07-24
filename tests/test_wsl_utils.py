"""run_wsl_streaming's stall-detection path (see wsl_utils.py) - regression
coverage for a real incident: two concurrent decay runs got killed
externally mid-iteration, and the old `for line in proc.stdout` loop never
noticed (should_stop() was only ever checked *after* a new line arrived,
so with zero further output nothing ran at all - not even a Stop request),
leaving the whole scenario sweep silently hung for over an hour.
"""
import subprocess
import threading

from guvcfd import wsl_utils


class _FakeStdout:
    """A process pipe that yields a scripted list of lines, then blocks
    indefinitely (simulating a stalled/dead pipe) until the test or the
    code under test closes it - never a real subprocess.
    """

    def __init__(self, lines):
        self._lines = list(lines)
        self._closed = threading.Event()

    def readline(self):
        if self._lines:
            return self._lines.pop(0) + "\n"
        self._closed.wait(timeout=30)  # "stuck" until terminate()/kill() closes us
        return ""

    def close_now(self):
        self._closed.set()


class _FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = _FakeStdout(lines)
        self.args = ["wsl", "fake"]
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self.stdout.close_now()

    def kill(self):
        self.killed = True
        self.stdout.close_now()

    def wait(self, timeout=None):
        return self.returncode


def test_should_stop_is_checked_even_with_zero_further_output(monkeypatch):
    monkeypatch.setattr(wsl_utils, "_STALL_POLL_INTERVAL_S", 0.02)
    # One real line first (so this doesn't look like a launch failure and
    # retry), then the pipe stalls - matching the real incident, which
    # happened well into an already-running solve, not at launch.
    fake_proc = _FakeProc(lines=["Time = 1"])
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: fake_proc)

    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] >= 3

    result = wsl_utils.run_wsl_streaming("solver", "/tmp/case", should_stop=should_stop)
    assert fake_proc.terminated
    assert calls["n"] >= 3
    assert result.returncode == -15


def test_stall_timeout_gives_up_and_terminates_without_should_stop(monkeypatch):
    monkeypatch.setattr(wsl_utils, "_STALL_POLL_INTERVAL_S", 0.01)
    fake_proc = _FakeProc(lines=["Time = 1"])
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: fake_proc)

    messages = []
    result = wsl_utils.run_wsl_streaming(
        "solver", "/tmp/case", on_line=messages.append, stall_timeout=0.05)
    assert fake_proc.terminated
    assert any("no output for" in m for m in messages)
    assert result.returncode == -15


def test_normal_output_and_clean_exit_is_unaffected(monkeypatch):
    monkeypatch.setattr(wsl_utils, "_STALL_POLL_INTERVAL_S", 0.05)

    class _CleanExitStdout(_FakeStdout):
        def readline(self):
            if self._lines:
                return self._lines.pop(0) + "\n"
            return ""  # pipe closes normally, like a process that actually finished

    fake_proc = _FakeProc(lines=[])
    fake_proc.stdout = _CleanExitStdout(["Time = 1", "Time = 2"])
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: fake_proc)

    collected = []
    result = wsl_utils.run_wsl_streaming("solver", "/tmp/case", on_line=collected.append)
    assert collected == ["Time = 1", "Time = 2"]
    assert result.returncode == 0
    assert not fake_proc.terminated
    assert not fake_proc.killed
