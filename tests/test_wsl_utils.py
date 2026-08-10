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


def test_kill_pattern_is_scoped_to_this_calls_own_cwd(monkeypatch):
    # Regression: a bare `pkill -f pimpleFoam` kills every same-named
    # solver process system-wide - fine with one solve at a time, wrong
    # once concurrent ACH/Z combinations can each be running their own
    # "pimpleFoam" at once. The kill must be scoped to the specific case
    # directory this run_wsl_streaming call was launched in.
    monkeypatch.setattr(wsl_utils, "_STALL_POLL_INTERVAL_S", 0.02)
    fake_proc = _FakeProc(lines=["Time = 1"])
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: fake_proc)

    kill_calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: kill_calls.append(cmd))

    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] >= 3

    wsl_utils.run_wsl_streaming(
        "pimpleFoam", "/mnt/project/Z6_ACH3", should_stop=should_stop, kill_pattern="pimpleFoam")

    assert len(kill_calls) == 1
    script = kill_calls[0][-1]
    assert "/mnt/project/Z6_ACH3" in script
    assert "pimpleFoam" in script
    assert "cwd" in script  # scoped by /proc/$p/cwd, not a bare name match


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


# --- SSH transport (GUVCFD_WSL_TRANSPORT=ssh) - same contract, mocked at
# the paramiko Channel/SSHClient level instead of subprocess.Popen. See
# project_wsl_ssh_paramiko_migration memory for why this transport exists.

class _FakeChannel:
    """Mimics paramiko.Channel just enough for _run_wsl_streaming_ssh:
    recv() yields a scripted list of lines (each returned as one chunk,
    already newline-terminated) then blocks until close()/a real EOF is
    simulated - mirrors _FakeStdout's role for the subprocess tests above.
    """

    def __init__(self, lines, exit_status=0):
        self._lines = [line.encode() + b"\n" for line in lines]
        self._closed = threading.Event()
        self._exit_status = exit_status
        self.close_called = False

    def set_combine_stderr(self, value):
        pass

    def exec_command(self, cmd):
        self.cmd = cmd

    def recv(self, n):
        if self._lines:
            return self._lines.pop(0)
        self._closed.wait(timeout=30)  # "stuck" until the code under test closes us
        return b""

    def exit_status_ready(self):
        return self._closed.is_set()

    def recv_exit_status(self):
        return self._exit_status

    def close(self):
        self.close_called = True
        self._closed.set()


class _FakeCleanExitChannel(_FakeChannel):
    """Like _FakeChannel, but recv() returns b"" (EOF) once the scripted
    lines run out, instead of blocking - a process that actually finished
    on its own, matching _CleanExitStdout's role above."""

    def recv(self, n):
        if self._lines:
            return self._lines.pop(0)
        self._closed.set()
        return b""


class _FakeTransport:
    def __init__(self, channel):
        self._channel = channel

    def open_session(self):
        return self._channel

    def is_active(self):
        return True


class _FakeSSHClient:
    def __init__(self, channel):
        self._channel = channel

    def get_transport(self):
        return _FakeTransport(self._channel)


def test_ssh_should_stop_is_checked_even_with_zero_further_output(monkeypatch):
    monkeypatch.setattr(wsl_utils, "_WSL_TRANSPORT", "ssh")
    monkeypatch.setattr(wsl_utils, "_STALL_POLL_INTERVAL_S", 0.02)
    monkeypatch.setattr(wsl_utils, "_EXIT_STATUS_GRACE_S", 0.05)
    fake_channel = _FakeChannel(lines=["Time = 1"])
    monkeypatch.setattr(wsl_utils, "_get_ssh_client", lambda: _FakeSSHClient(fake_channel))

    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] >= 3

    result = wsl_utils.run_wsl_streaming("solver", "/tmp/case", should_stop=should_stop)
    assert fake_channel.close_called
    assert calls["n"] >= 3
    assert result.returncode == 0  # fake channel's scripted exit status


def test_ssh_stall_timeout_gives_up_and_terminates_without_should_stop(monkeypatch):
    monkeypatch.setattr(wsl_utils, "_WSL_TRANSPORT", "ssh")
    monkeypatch.setattr(wsl_utils, "_STALL_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(wsl_utils, "_EXIT_STATUS_GRACE_S", 0.05)
    fake_channel = _FakeChannel(lines=["Time = 1"])
    monkeypatch.setattr(wsl_utils, "_get_ssh_client", lambda: _FakeSSHClient(fake_channel))

    messages = []
    result = wsl_utils.run_wsl_streaming(
        "solver", "/tmp/case", on_line=messages.append, stall_timeout=0.05)
    assert fake_channel.close_called
    assert any("no output for" in m for m in messages)
    assert result.returncode == 0


def test_ssh_normal_output_and_clean_exit_is_unaffected(monkeypatch):
    monkeypatch.setattr(wsl_utils, "_WSL_TRANSPORT", "ssh")
    monkeypatch.setattr(wsl_utils, "_STALL_POLL_INTERVAL_S", 0.05)
    fake_channel = _FakeCleanExitChannel(["Time = 1", "Time = 2"])
    monkeypatch.setattr(wsl_utils, "_get_ssh_client", lambda: _FakeSSHClient(fake_channel))

    collected = []
    result = wsl_utils.run_wsl_streaming("solver", "/tmp/case", on_line=collected.append)
    assert collected == ["Time = 1", "Time = 2"]
    assert result.returncode == 0
    assert not fake_channel.close_called


def test_get_ssh_client_reuses_live_connection(monkeypatch):
    fake_client = _FakeSSHClient(_FakeChannel([]))
    wsl_utils._ssh_client_cache["client"] = fake_client
    try:
        assert wsl_utils._get_ssh_client() is fake_client
    finally:
        wsl_utils._ssh_client_cache["client"] = None


def test_get_ssh_client_reconnects_when_transport_is_dead(monkeypatch):
    class _DeadTransport:
        def is_active(self):
            return False

    class _DeadClient:
        def __init__(self):
            self.closed = False

        def get_transport(self):
            return _DeadTransport()

        def close(self):
            self.closed = True

    dead_client = _DeadClient()
    wsl_utils._ssh_client_cache["client"] = dead_client

    fresh_client = object()
    connect_calls = []

    class _FakeNewTransport:
        def set_keepalive(self, interval):
            pass

    class _FakeNewClient:
        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, ip, **kwargs):
            connect_calls.append((ip, kwargs))

        def get_transport(self):
            return _FakeNewTransport()

    monkeypatch.setattr(wsl_utils, "_resolve_wsl_ip", lambda: "172.30.1.1")
    monkeypatch.setattr(wsl_utils.paramiko, "SSHClient", _FakeNewClient)
    monkeypatch.setattr(wsl_utils.paramiko, "AutoAddPolicy", lambda: None)
    # Never spawn a REAL wsl.exe subprocess from a unit test - would hang/
    # fail outright on any machine without WSL (e.g. CI).
    keeper_calls = []
    monkeypatch.setattr(wsl_utils, "_ensure_wsl_keeper_alive", lambda: keeper_calls.append(1))

    try:
        result = wsl_utils._get_ssh_client()
        assert dead_client.closed
        assert connect_calls and connect_calls[0][0] == "172.30.1.1"
        assert isinstance(result, _FakeNewClient)
        assert keeper_calls == [1]
    finally:
        wsl_utils._ssh_client_cache["client"] = None


def test_ensure_wsl_keeper_alive_spawns_once_and_skips_if_already_running(monkeypatch):
    """Idempotency (2026-08-07): must not spawn a second `sleep infinity`
    process while a previous one is still running - only spawns again if
    the cached process has actually exited (poll() is not None)."""
    popen_calls = []

    class _FakeProcess:
        def __init__(self):
            self._exited = False

        def poll(self):
            return None if not self._exited else 0

    monkeypatch.setattr(wsl_utils, "_wsl_keeper_process", None)
    monkeypatch.setattr(wsl_utils.subprocess, "Popen", lambda *a, **k: popen_calls.append(1) or _FakeProcess())

    wsl_utils._ensure_wsl_keeper_alive()
    assert len(popen_calls) == 1

    # Still running - a second call must NOT spawn another one.
    wsl_utils._ensure_wsl_keeper_alive()
    assert len(popen_calls) == 1

    # Simulate it having died - the next call should spawn a fresh one.
    wsl_utils._wsl_keeper_process._exited = True
    wsl_utils._ensure_wsl_keeper_alive()
    assert len(popen_calls) == 2


def test_get_sftp_client_sets_channel_timeout_on_a_freshly_opened_client(monkeypatch):
    """A silently-dead connection (no RST/FIN) would otherwise hang an SFTP
    open/read/write forever - see _SFTP_OP_TIMEOUT_S's comment. Every fresh
    SFTP client must get its channel's socket timeout set before use."""
    wsl_utils._sftp_client_local.sftp = None
    timeout_calls = []

    class _FakeChannel:
        def settimeout(self, value):
            timeout_calls.append(value)

    class _FakeSFTP:
        def get_channel(self):
            return _FakeChannel()

    class _FakeClient:
        def open_sftp(self):
            return _FakeSFTP()

    monkeypatch.setattr(wsl_utils, "_get_ssh_client", lambda: _FakeClient())

    result = wsl_utils._get_sftp_client()
    assert isinstance(result, _FakeSFTP)
    assert timeout_calls == [wsl_utils._SFTP_OP_TIMEOUT_S]
    wsl_utils._sftp_client_local.sftp = None


def test_get_sftp_client_gives_each_thread_its_own_instance(monkeypatch):
    """2026-08-07 thread-safety fix - regression guard for the actual
    behavioral change: different threads must never share the same
    SFTPClient object (the suspected root cause of concurrency-related
    production failures - see _get_sftp_client's own docstring)."""
    wsl_utils._sftp_client_local.sftp = None
    open_count = {"n": 0}

    class _FakeChannel:
        def settimeout(self, value):
            pass

    class _FakeSFTP:
        def __init__(self, n):
            self.n = n

        def get_channel(self):
            return _FakeChannel()

    class _FakeClient:
        def open_sftp(self):
            open_count["n"] += 1
            return _FakeSFTP(open_count["n"])

    monkeypatch.setattr(wsl_utils, "_get_ssh_client", lambda: _FakeClient())

    results = {}

    def worker(name):
        # Call twice - within ONE thread, the second call must reuse the
        # same instance (still cached per-thread, not reconnecting every
        # time).
        first = wsl_utils._get_sftp_client()
        second = wsl_utils._get_sftp_client()
        results[name] = (first.n, second.n)

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 5
    all_ns = [ns for (a, b) in results.values() for ns in (a, b)]
    # Each thread got its own open_sftp() call (5 distinct instances total,
    # not 1 shared one) ...
    assert len(set(ns for a, b in results.values() for ns in (a,))) == 5
    # ... and each thread's OWN two calls returned the SAME instance
    # (per-thread caching still works, just no longer cross-thread).
    for a, b in results.values():
        assert a == b
    wsl_utils._sftp_client_local.sftp = None


class _FakeReadFile:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"hello"


def test_read_wsl_text_ssh_only_discards_sftp_on_first_failure(monkeypatch):
    """Graduated recovery (2026-08-07, real sweep failure): an SFTP channel
    dying doesn't mean the whole SSH transport is dead - the remote
    sftp-server subprocess can die on its own. The first failure should
    only discard the SFTP client (so the next attempt reuses the transport
    if it's still genuinely alive) - only a SECOND consecutive failure
    escalates to discarding the SSH client too, forcing a full reconnect.
    """
    monkeypatch.setattr(wsl_utils, "_WSL_RETRY_DELAY_S", 0)
    discard_order = []
    monkeypatch.setattr(wsl_utils, "_discard_dead_sftp_client", lambda: discard_order.append("sftp"))
    monkeypatch.setattr(wsl_utils, "_discard_dead_ssh_client", lambda: discard_order.append("ssh"))

    attempts = {"n": 0}

    def fake_get_sftp_client():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OSError("Server connection dropped")
        return type("S", (), {"open": lambda self, p, m: _FakeReadFile()})()

    monkeypatch.setattr(wsl_utils, "_get_sftp_client", fake_get_sftp_client)

    result = wsl_utils._read_wsl_text_ssh("/some/path")
    assert result == "hello"
    assert discard_order == ["sftp", "sftp", "ssh"]


def test_write_wsl_text_ssh_only_discards_sftp_on_first_failure(monkeypatch):
    monkeypatch.setattr(wsl_utils, "_WSL_RETRY_DELAY_S", 0)
    discard_order = []
    monkeypatch.setattr(wsl_utils, "_discard_dead_sftp_client", lambda: discard_order.append("sftp"))
    monkeypatch.setattr(wsl_utils, "_discard_dead_ssh_client", lambda: discard_order.append("ssh"))

    attempts = {"n": 0}
    written = []

    class _FakeWriteFile:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def write(self, content):
            written.append(content)

    def fake_get_sftp_client():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OSError("Server connection dropped")
        return type("S", (), {"open": lambda self, p, m: _FakeWriteFile()})()

    monkeypatch.setattr(wsl_utils, "_get_sftp_client", fake_get_sftp_client)

    wsl_utils._write_wsl_text_ssh("/some/path", "content")
    assert written == ["content"]
    assert discard_order == ["sftp", "sftp", "ssh"]


def test_read_wsl_text_ssh_eventually_escalates_and_raises_with_no_recovery(monkeypatch):
    """If it never recovers, the final error message should still surface
    (not swallowed), and every attempt past the first should have escalated
    to a full SSH discard too - not stuck cheap-retrying forever."""
    monkeypatch.setattr(wsl_utils, "_WSL_RETRY_DELAY_S", 0)
    discard_order = []
    monkeypatch.setattr(wsl_utils, "_discard_dead_sftp_client", lambda: discard_order.append("sftp"))
    monkeypatch.setattr(wsl_utils, "_discard_dead_ssh_client", lambda: discard_order.append("ssh"))

    def always_fails():
        raise OSError("Server connection dropped")

    monkeypatch.setattr(wsl_utils, "_get_sftp_client", always_fails)

    try:
        wsl_utils._read_wsl_text_ssh("/some/path")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "Server connection dropped" in str(e)
    # First attempt: sftp-only. Every attempt after that: both.
    assert discard_order[0] == "sftp"
    assert discard_order.count("ssh") == wsl_utils._WSL_RETRY_ATTEMPTS
