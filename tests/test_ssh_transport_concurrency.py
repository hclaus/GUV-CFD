r"""Red/green concurrency stress test for the SSH/paramiko WSL transport's
per-thread SFTP fix (see wsl_utils._get_sftp_client's own docstring for
the full thread-safety hypothesis this is built to test).

**Not run as part of the normal `pytest tests/` suite** - it needs a
real, disposable sshd, not mocks, to catch genuine paramiko-level
concurrency behavior a mock would hide entirely. Skipped unless
GUVCFD_SSH_TEST_HOST is set.

**CRITICAL SAFETY RULE**: this must NEVER point at the shared production
WSL instance while it's doing real work - a live simulation can be
running on it at any time, and this test deliberately hammers it with
concurrent load (100 directories x create/list/write/read/copy/delete,
across 6 threads by default) for an extended duration, competing for real
CPU/disk I/O even if it never touches the same files. Point it at a
disposable target only:
  - a dedicated scratch WSL distro set up specifically for this
    (`wsl --install -d Ubuntu --name guvcfd-ssh-test` or similar, its own
    openssh-server + dedicated test keypair, entirely separate from the
    production distro/keypair in "Linux installation.md"), or
  - any other throwaway sshd on a non-default port you control, or
  - the production WSL instance ONLY while it is genuinely idle (nothing
    else running - confirm with `wsl -e ps aux | grep -iE
    "pimpleFoam|simpleFoam"` first) - never alongside a real sweep.

How to run once a safe target is confirmed idle (PowerShell):
    $env:GUVCFD_SSH_TEST_HOST = "172.x.x.x"      # the target's IP
    $env:GUVCFD_SSH_TEST_PORT = "22"              # optional, defaults to 22
    $env:GUVCFD_SSH_TEST_USER = "hclaus"          # optional, defaults to hclaus
    $env:GUVCFD_SSH_TEST_KEY = "C:\path\to\key"   # optional, defaults to guvcfd_wsl_key
    $env:GUVCFD_SSH_TEST_DIR = "/home/hclaus/ssh_concurrency_test"  # scratch dir, must already exist and be empty-ish
    uv run pytest tests/test_ssh_transport_concurrency.py -v -s

Each of the 100 (default) directories goes through: mkdir -p, ls (a
"read" of the freshly-created directory), write a short file into it,
read that file back and verify content, cp -r the whole directory,
verify the copy's own file content too, then rm -rf both original and
copy - mkdir/ls/cp/rm exercise wsl_utils.run_wsl_or_raise (exec_command,
already gets a fresh channel per call, not expected to be the fragile
path); write/read exercise wsl_utils.write_wsl_text/read_wsl_text (SFTP,
the path this whole investigation is about).

Tests BOTH implementations in the same run for a direct comparison:
- "new": the real, current wsl_utils._get_sftp_client (per-thread SFTP
  channel - the actual production code, unchanged from what ships).
- "old": a REFERENCE-ONLY reimplementation of the pre-fix shared-
  SFTPClient design, defined in THIS TEST FILE ONLY (nothing in
  production code was reverted) - since nothing on this branch has been
  committed yet, there's no clean prior commit to check out for the red
  side of the comparison, so the old behavior is reproduced locally here
  instead. The "old" run is informational (concurrency races are often
  timing-dependent, so a clean pass on a given run doesn't disprove the
  hypothesis) - only the "new" run is a hard pass/fail gate.
"""
import os
import threading
import time
import uuid

import pytest

from guvcfd import wsl_utils

_HOST = os.environ.get("GUVCFD_SSH_TEST_HOST")
_PORT = int(os.environ.get("GUVCFD_SSH_TEST_PORT", "22"))
_USER = os.environ.get("GUVCFD_SSH_TEST_USER", "hclaus")
_KEY = os.environ.get("GUVCFD_SSH_TEST_KEY", os.path.expanduser("~/.ssh/guvcfd_wsl_key"))
_TEST_DIR = os.environ.get("GUVCFD_SSH_TEST_DIR")

# Matches the user's own proposed workload.
_N_DIRECTORIES = int(os.environ.get("GUVCFD_SSH_TEST_N_DIRS", "100"))
_N_THREADS = int(os.environ.get("GUVCFD_SSH_TEST_N_THREADS", "6"))

pytestmark = pytest.mark.skipif(
    not _HOST or not _TEST_DIR,
    reason="Set GUVCFD_SSH_TEST_HOST and GUVCFD_SSH_TEST_DIR (a confirmed-idle, "
           "disposable SSH target) to run this - see module docstring.",
)


@pytest.fixture(autouse=True)
def _point_at_test_target(monkeypatch):
    """Redirects wsl_utils' connection parameters at the test target for
    the duration of this test module only. Bypasses _resolve_wsl_ip's own
    `wsl -e hostname -I` call entirely (the target may not even be this
    machine's own wsl.exe-controlled distro).
    """
    monkeypatch.setattr(wsl_utils, "_WSL_TRANSPORT", "ssh")
    monkeypatch.setattr(wsl_utils, "_resolve_wsl_ip", lambda: _HOST)
    monkeypatch.setattr(wsl_utils, "_SSH_PORT", _PORT)
    monkeypatch.setattr(wsl_utils, "_SSH_USER", _USER)
    monkeypatch.setattr(wsl_utils, "_SSH_KEY_PATH", _KEY)
    wsl_utils._ssh_client_cache["client"] = None
    wsl_utils._sftp_client_local.sftp = None
    yield
    wsl_utils._ssh_client_cache["client"] = None
    wsl_utils._sftp_client_local.sftp = None


# --- Reference-only reproduction of the OLD (pre-fix) shared-SFTPClient
# design, for the "red" side of the comparison. NOT used by production
# code - see wsl_utils._get_sftp_client's own docstring for the real,
# current (per-thread) implementation. ---

_old_sftp_cache = {"sftp": None}
_old_sftp_lock = threading.Lock()


def _old_get_sftp_client():
    with _old_sftp_lock:
        sftp = _old_sftp_cache["sftp"]
        if sftp is not None:
            return sftp
        client = wsl_utils._get_ssh_client()
        new_sftp = client.open_sftp()
        new_sftp.get_channel().settimeout(wsl_utils._SFTP_OP_TIMEOUT_S)
        _old_sftp_cache["sftp"] = new_sftp
        return new_sftp


def _old_discard_dead_sftp_client():
    with _old_sftp_lock:
        sftp = _old_sftp_cache["sftp"]
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
            _old_sftp_cache["sftp"] = None


def _reset_old_cache():
    with _old_sftp_lock:
        _old_sftp_cache["sftp"] = None


def _run_one_directory_workflow(index):
    """One full directory lifecycle - mkdir, ls, write a file, read it
    back, cp -r, verify the copy, rm -rf both. Returns None on success,
    or a description of what went wrong (exception, or a content
    mismatch - the "corrupted read" failure mode the shared-client
    hypothesis specifically predicts, not just outright exceptions).
    """
    d = f"{_TEST_DIR}/dir_{index}_{uuid.uuid4().hex}"
    d_copy = f"{d}_copy"
    try:
        wsl_utils.run_wsl_or_raise(f'mkdir -p "{d}"', _TEST_DIR, "mkdir")
        wsl_utils.run_wsl_or_raise(f'ls -la "{d}"', _TEST_DIR, "ls")  # the "read them" step

        content = f"dir-{index}-{uuid.uuid4().hex}\n"
        file_path = f"{d}/note.txt"
        wsl_utils.write_wsl_text(file_path, content)
        readback = wsl_utils.read_wsl_text(file_path)
        if readback != content:
            return f"dir {index}: CORRUPTED READ - wrote {content!r}, read back {readback!r}"

        wsl_utils.run_wsl_or_raise(f'cp -r "{d}" "{d_copy}"', _TEST_DIR, "cp -r")
        copy_readback = wsl_utils.read_wsl_text(f"{d_copy}/note.txt")
        if copy_readback != content:
            return f"dir {index}: COPY MISMATCH - expected {content!r}, got {copy_readback!r}"

        wsl_utils.run_wsl_or_raise(f'rm -rf "{d}" "{d_copy}"', _TEST_DIR, "rm -rf")
    except Exception as e:
        return f"dir {index}: exception {type(e).__name__}: {e}"
    return None


def _run_workload(n_dirs, n_threads):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    failures = []
    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = [pool.submit(_run_one_directory_workflow, i) for i in range(n_dirs)]
        for f in as_completed(futures):
            result = f.result()
            if result:
                failures.append(result)
    return failures


def test_new_per_thread_sftp_has_zero_failures_under_concurrency():
    """The actual acceptance gate: the current, real production code
    (per-thread SFTP channel) must survive the full 100-directory/
    6-thread workload with zero failures."""
    t0 = time.time()
    failures = _run_workload(_N_DIRECTORIES, _N_THREADS)
    elapsed = time.time() - t0
    print(f"\n[new/per-thread] {_N_DIRECTORIES} dirs x {_N_THREADS} threads in {elapsed:.1f}s, "
          f"{len(failures)} failure(s)")
    assert failures == [], (f"{len(failures)} failure(s) with the NEW per-thread implementation "
                             f"(should be zero):\n" + "\n".join(failures[:15]))


def test_old_shared_sftp_client_comparison(monkeypatch):
    """Informational, NOT a hard gate - concurrency races are often
    timing-dependent, so a clean run here on a given machine/moment
    doesn't disprove the hypothesis, only a reproduced failure confirms
    it. Runs the IDENTICAL workload against the old shared-client
    reference implementation and reports the failure count for direct
    comparison against the new implementation's own (expected zero)
    result above.
    """
    monkeypatch.setattr(wsl_utils, "_get_sftp_client", _old_get_sftp_client)
    monkeypatch.setattr(wsl_utils, "_discard_dead_sftp_client", _old_discard_dead_sftp_client)
    _reset_old_cache()
    t0 = time.time()
    failures = _run_workload(_N_DIRECTORIES, _N_THREADS)
    elapsed = time.time() - t0
    print(f"\n[old/shared-client] {_N_DIRECTORIES} dirs x {_N_THREADS} threads in {elapsed:.1f}s, "
          f"{len(failures)} failure(s) (informational only)")
    if failures:
        print("Sample failures:\n" + "\n".join(failures[:15]))
    _reset_old_cache()
