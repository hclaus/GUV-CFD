"""Shared helpers for shelling out to WSL/OpenFOAM binaries, used by both
run_pipeline.py (decay scenario) and steady_state_pipeline.py (continuous
source scenario).
"""
import atexit
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

import paramiko

OPENFOAM_BASHRC = "/usr/lib/openfoam/openfoam2412/etc/bashrc"

# Which transport talks to WSL: "subprocess" (spawn a fresh wsl.exe per
# command - today's long-standing default, kept as the default here too
# until the "ssh" path is proven in real use) or "ssh" (persistent
# paramiko connection - see "Linux installation.md" at the repo root for
# the one-time WSL-side setup this requires: openssh-server installed and
# enabled, a dedicated keypair in authorized_keys). Overridable via env
# var specifically so it can be flipped without a code change/rollback if
# the new path misbehaves in practice - this repo has already been burned
# once this session by subtle WSL-boundary bugs, so the escape hatch is
# deliberate, not paranoia.
_WSL_TRANSPORT = os.environ.get("GUVCFD_WSL_TRANSPORT", "subprocess")

# Dedicated to this integration - NOT the user's personal SSH key. See
# "Linux installation.md" for how this was generated and installed into
# WSL's authorized_keys.
_SSH_KEY_PATH = os.path.expanduser("~/.ssh/guvcfd_wsl_key")
_SSH_USER = "hclaus"
_SSH_PORT = 22

_ssh_client_cache = {"client": None}
_ssh_client_lock = threading.Lock()

# All SSH channels (SFTP + exec, including run_wsl_streaming's) ride the
# SAME shared Transport/TCP connection (see _get_ssh_client) - but sshd
# caps the number of channels open AT ONCE on a single connection via
# MaxSessions. A concurrency stress test on 2026-08-10 (100 dirs x 6
# threads, each holding one persistent per-thread SFTP channel PLUS
# opening short-lived exec channels for mkdir/ls/cp/rm) reproduced real,
# repeatable "Secsh channel N open FAILED: open failed: Connect failed"
# errors from paramiko once concurrently-open channels exceeded the
# then-default cap (10) - a genuinely different root cause than the
# 2026-08-07 shared-SFTPClient thread-safety hypothesis (which the
# per-thread-channel fix in _get_sftp_client already addresses; this
# semaphore is a separate, additional fix for a separate problem).
# MaxSessions was raised to 40 server-side that same day (see "Linux
# installation.md") specifically to cover real sweeps' up to 9 concurrent
# threads (scenario_runs._MAX_CONCURRENT_SOLVES), each potentially
# wanting a persistent SFTP channel plus occasional exec channels
# (including run_wsl_streaming's, up to 2 per decay group - UV-on +
# UV-off control). 30 stays comfortably under the server's 40, leaving
# headroom rather than sitting flush against the cap. Bounding the app's
# OWN channel usage converts what would otherwise be a hard failure into
# orderly waiting instead - a client-side safety net on top of the
# server-side fix, not a substitute for it.
_MAX_CONCURRENT_SSH_CHANNELS = 30
_ssh_channel_semaphore = threading.BoundedSemaphore(_MAX_CONCURRENT_SSH_CHANNELS)

# WSL2 tracks whether an instance is "in use" by whether any wsl.exe
# process is currently attached to it - NOT by inbound connections into it
# (like our persistent paramiko SSH session, or sshd itself). Confirmed
# directly (2026-08-07, via `wsl -e journalctl -u ssh`): sshd received a
# clean SIGTERM from systemd ~20s after starting - WSL2's own idle-VM-
# shutdown tearing the whole instance down, not a network drop or OOM
# kill. _resolve_wsl_ip's own `wsl -e hostname -I` call starts the VM,
# runs, and exits immediately, leaving nothing attached from Windows's
# side - so a few seconds later WSL's idle timer fires and takes sshd down
# with it. Only relevant to the ssh transport - the subprocess transport's
# per-command wsl.exe spawn is naturally immune (every call transparently
# restarts the VM if it's gone, so a cold/idle VM between calls is a
# non-event there).
_wsl_keeper_process = None
_wsl_keeper_lock = threading.Lock()


def _ensure_wsl_keeper_alive():
    """Idempotently spawn (once) a long-lived `wsl -e sleep infinity`
    process and hold its handle for this app's lifetime, so WSL2 always
    sees at least one attached wsl.exe process and never considers the
    instance idle - a belt-and-suspenders code-level fix that doesn't
    depend on the machine's own .wslconfig (vmIdleTimeout) being set up
    correctly. Called once an SSH connection actually needs to survive
    across gaps with no other wsl.exe activity (see _get_ssh_client).
    """
    global _wsl_keeper_process
    with _wsl_keeper_lock:
        if _wsl_keeper_process is not None and _wsl_keeper_process.poll() is None:
            return
        _wsl_keeper_process = subprocess.Popen(
            ["wsl", "-e", "sleep", "infinity"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def _terminate_wsl_keeper():
    global _wsl_keeper_process
    with _wsl_keeper_lock:
        if _wsl_keeper_process is not None:
            try:
                _wsl_keeper_process.terminate()
            except Exception:
                pass
            _wsl_keeper_process = None


atexit.register(_terminate_wsl_keeper)


def _resolve_wsl_ip():
    """Query WSL's current IP via a single `wsl -e hostname -I` call.

    WSL2 uses standard NAT networking here (not "mirrored" mode), so its
    IP address can change across `wsl --shutdown`/restart cycles - this
    must be re-resolved each time a new connection is needed, never
    hardcoded or cached indefinitely. Still one `wsl.exe` spawn per
    (re)connect, but that's now rare (a persistent SSH connection is
    reused across every subsequent call) rather than once per command,
    which is what actually made the old transport fragile.
    """
    r = subprocess.run(["wsl", "-e", "hostname", "-I"], capture_output=True, text=True, timeout=15)
    parts = r.stdout.split()
    if r.returncode != 0 or not parts:
        raise RuntimeError(f"Could not resolve WSL's IP (wsl -e hostname -I): "
                            f"exit={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}")
    return parts[0]


def _get_ssh_client():
    """A connected paramiko.SSHClient, reconnecting if the cached one is
    missing or dead. Lazy and persistent - unlike the subprocess
    transport (a fresh wsl.exe process per call), this connects once and
    is reused across every subsequent run_wsl*/write_wsl_text/
    read_wsl_text call, until it actually drops (WSL restart, sleep/wake).

    Thread-safe: paramiko's Transport supports opening multiple channels
    concurrently from different threads over one connection (exactly what
    concurrent ACH-group solves need), but the connect-or-reconnect
    decision itself is guarded by a lock so concurrent callers don't race
    into opening duplicate connections when the cached one has just died.
    """
    with _ssh_client_lock:
        client = _ssh_client_cache["client"]
        if client is not None:
            transport = client.get_transport()
            if transport is not None and transport.is_active():
                return client
            try:
                client.close()
            except Exception:
                pass
            _ssh_client_cache["client"] = None

        # Attach the keeper BEFORE resolving the IP - _resolve_wsl_ip's own
        # `wsl -e hostname -I` call starts the VM, runs, and exits almost
        # immediately, and WSL's idle-shutdown timer can fire in the gap
        # right after that if nothing else is attached yet (see the keeper
        # functions' own docstring for the confirmed real incident this
        # guards against).
        _ensure_wsl_keeper_alive()
        ip = _resolve_wsl_ip()
        new_client = paramiko.SSHClient()
        new_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        new_client.connect(ip, port=_SSH_PORT, username=_SSH_USER,
                            key_filename=_SSH_KEY_PATH, timeout=15)
        # Confirmed directly (2026-08-07): a real multi-ACH-group sweep left
        # this connection genuinely idle for the length of a flow-convergence/
        # decay solve (which runs over its own channel, touching no SFTP/SSH
        # traffic at all) - long enough for something outside our control
        # (NAT/firewall/WSL's own networking) to silently kill the
        # underlying TCP connection. Neither side notices until something
        # actually tries to use it again - which is why the failure always
        # surfaces on the NEXT ACH group's first file operation, not on
        # whatever was "in flight" at the time of death. A periodic
        # keepalive gives paramiko a reason to touch the connection on its
        # own schedule even during a long solve, refreshing any NAT/firewall
        # state and surfacing a genuinely-dead connection promptly instead
        # of silently.
        new_client.get_transport().set_keepalive(30)
        _ssh_client_cache["client"] = new_client
        return new_client

# Observed repeatedly in practice: wsl.exe occasionally fails to launch/
# attach to the WSL side at all, returning a non-zero exit with *nothing*
# captured on either stdout or stderr - not a real command failure (every
# command actually reaching a shell inside WSL, success or failure, prints
# something). A couple of quick retries clears it; genuine failures always
# have real output and are never masked by this. Also shared by the SSH
# transport's own reconnect-on-drop retries (_read/_write_wsl_text_ssh) -
# widened from 2 to 4 (2026-08-07) after a real sweep's SSH connection
# dropped mid-run and exhausted the old 3-total-attempts/~3s budget before
# a full reconnect (fresh wsl.exe IP lookup + new TCP/SSH handshake) could
# complete - see the graduated-recovery comment in _write_wsl_text_ssh for
# why one of those attempts is spent cheaply (SFTP-channel-only reopen)
# before this budget's later attempts pay for a full reconnect.
_WSL_RETRY_ATTEMPTS = 4
_WSL_RETRY_DELAY_S = 1.5

# SFTP open/read/write ride a channel with no timeout by default - if the
# connection goes silently dead (no RST/FIN, just stops delivering data)
# instead of erroring cleanly, paramiko blocks forever with nothing to
# raise. Confirmed as a real incident (2026-08-07): the WSL-side
# sftp-server process was alive and idly waiting, not stuck - only the
# Windows-side paramiko client was blocked, which froze the whole app
# (a GUI whose main thread made the blocking call) with no error and no
# way to recover short of killing it. Scoped to the SFTP channel only via
# get_channel().settimeout() in _get_sftp_client - NOT applied to the
# whole SSH transport, since the long-running solver exec channels
# (run_wsl_streaming) are legitimately silent for many minutes and are
# already guarded by their own separate stall-timeout mechanism
# (_DEFAULT_STALL_TIMEOUT_S). A stall past this raises socket.timeout
# (TimeoutError, a subclass of OSError), so it's already caught by the
# existing `except (paramiko.SSHException, OSError)` in
# _write_wsl_text_ssh/_read_wsl_text_ssh - no separate except clause needed.
_SFTP_OP_TIMEOUT_S = 30

# How long run_wsl_streaming waits with NO new output at all before giving
# up and treating the process as stuck - see that function's docstring for
# the real incident (over an hour of total silence, no error, nothing
# written) this guards against. Iteration cadence for a live solver is
# normally seconds, not minutes, so this is a conservative threshold, not
# a tight one - false positives on a genuinely-still-working slow run
# should be rare.
_DEFAULT_STALL_TIMEOUT_S = 20 * 60
# How often the stall-detection loop wakes up to re-check should_stop()
# and the stall clock while waiting for the next line - not a sleep, just
# the queue.get() poll interval.
_STALL_POLL_INTERVAL_S = 5

# SSH transport only: grace period to wait for the remote command's real
# exit status after should_stop/stall-timeout asks it to die (via
# kill_pattern), before giving up on it and force-closing the channel -
# mirrors the subprocess transport's proc.wait(timeout=15)/proc.kill()
# escalation. Not consulted at all on a natural EOF (the channel already
# has its exit status ready by then, same as a subprocess path's clean
# exit never calling proc.kill() either).
_EXIT_STATUS_GRACE_S = 15


def _looks_like_wsl_launch_failure(returncode, stdout, stderr):
    return returncode != 0 and not stdout.strip() and not stderr.strip()


def wsl_path(unc_or_wsl_path):
    """Convert a \\\\wsl.localhost\\Distro\\... (or //wsl.localhost/Distro/...
    - Tk file dialogs return UNC paths with forward slashes on Windows)
    Windows UNC path to a native WSL /path. Passes through paths that are
    already native (no wsl.localhost marker) unchanged.
    """
    normalized = unc_or_wsl_path.replace("\\", "/")
    if "wsl.localhost" not in normalized.lower():
        return unc_or_wsl_path
    parts = normalized.split("/")
    idx = next(i for i, p in enumerate(parts) if p.lower() == "wsl.localhost")
    return "/" + "/".join(parts[idx + 2:])


def windows_path_to_wsl_mnt(windows_path):
    """Convert a native Windows path (C:\\... or C:/...) to its WSL drvfs
    mount equivalent (/mnt/c/...), for READING from a WSL-native command.

    Not the same direction as wsl_path(): that handles paths already inside
    the WSL filesystem accessed from Windows. This is for the opposite case
    - e.g. copying a repo template file into a case directory - and it
    matters because the two directions aren't equally reliable. Confirmed
    2026-08-03: creating a brand-new directory/file from Windows via
    pathlib.Path.mkdir()/touch()/shutil.copy() through \\\\wsl.localhost\\...
    is NOT reliably durable to the actual WSL filesystem (reproduced even
    on a freshly-restarted WSL VM, not a transient cache lag - the entry
    was simply never there when read back via a native `wsl` command,
    including after a full VM restart). Reading an existing Windows-side
    file from WSL via /mnt/c/... (drvfs) doesn't have this problem - it's
    the same long-established passthrough WSL has always used for the
    reverse direction. So: create any file/directory a subsequent
    WSL-native command depends on (blockMesh, etc.) via a WSL-native
    command, using this helper to reach Windows-side template sources
    rather than writing into the case dir from the Windows side first.
    """
    normalized = str(windows_path).replace("\\", "/")
    if len(normalized) >= 2 and normalized[1] == ":":
        drive = normalized[0].lower()
        rest = normalized[2:].lstrip("/")
        return f"/mnt/{drive}/{rest}"
    return normalized


# Per-thread, NOT shared across threads - see _get_sftp_client's own
# docstring for why a single shared SFTPClient is suspected of causing
# real production failures under concurrent use.
_sftp_client_local = threading.local()


def _get_sftp_client():
    """A connected paramiko.SFTPClient, ONE PER THREAD - opened lazily on
    a thread's first use, reconnecting if that thread's own cached one
    (or the SSH connection it rides on) is dead.

    2026-08-07 thread-safety hypothesis (see the SSH transport plan's own
    "Concrete thread-safety hypothesis" section): the OLD design cached
    ONE SFTPClient shared by every thread, guarded only at the
    get-or-create step - actual sftp.open(...).read()/.write() calls
    happened outside any lock, on the same shared object, from up to 9
    concurrent threads in real production use
    (scenario_runs._MAX_CONCURRENT_SOLVES).
    Paramiko's Transport genuinely supports many concurrent *channels*,
    but a single SFTPClient's own internal state (request-id counter,
    pending-reply tracking) isn't documented as safe for concurrent
    multi-thread use without external synchronization - a plausible
    explanation for observed failures that looked like dead connections
    but weren't fully explained by the network-layer causes already found
    and fixed (wsl-pro.service crash loop, no SFTP channel timeout).

    Fix: each thread gets its OWN SFTPClient/channel, all riding the SAME
    shared Transport (see _get_ssh_client - that part stays correctly
    shared/reused, only the SFTP layer changes) - still one real TCP/SSH
    connection, just channel-per-thread instead of client-per-shared-
    object. threading.local() gives each thread an isolated slot with no
    cross-thread contention at all, so no lock is needed here (unlike the
    old design's now-removed _sftp_client_lock).

    No proactive liveness probe here (unlike _get_ssh_client's
    transport.is_active() check, there's no free/local equivalent for
    SFTP) - a cleanly-dead session surfaces naturally as an exception on
    first real use, handled by _write_wsl_text_ssh/_read_wsl_text_ssh's
    own retry-and-reconnect loop. A *silently* dead one (connection stops
    delivering data without ever sending RST/FIN) would otherwise hang
    forever with nothing to raise - see _SFTP_OP_TIMEOUT_S's comment -
    which is why the channel gets an explicit timeout below rather than
    relying on that surfacing itself.
    """
    sftp = getattr(_sftp_client_local, "sftp", None)
    if sftp is not None:
        return sftp
    client = _get_ssh_client()
    # Held for as long as this thread's SFTP channel stays open (released
    # in _discard_dead_sftp_client), not just for this call - see
    # _ssh_channel_semaphore's own comment for why (MaxSessions caps
    # concurrently OPEN channels, not open attempts).
    _ssh_channel_semaphore.acquire()
    try:
        new_sftp = client.open_sftp()
        new_sftp.get_channel().settimeout(_SFTP_OP_TIMEOUT_S)
    except Exception:
        _ssh_channel_semaphore.release()
        raise
    _sftp_client_local.sftp = new_sftp
    return new_sftp


def _discard_dead_sftp_client():
    sftp = getattr(_sftp_client_local, "sftp", None)
    if sftp is not None:
        _ssh_channel_semaphore.release()
        try:
            sftp.close()
        except Exception:
            pass
        _sftp_client_local.sftp = None


def _write_wsl_text_subprocess(wsl_target_path, content):
    r = subprocess.run(["wsl", "-e", "bash", "-lc", f'cat > "{wsl_target_path}"'],
                        input=content, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"writing {wsl_target_path} via WSL failed: {r.stderr}")


def _write_wsl_text_ssh(wsl_target_path, content):
    """SFTP equivalent of _write_wsl_text_subprocess - genuine native
    Linux filesystem I/O on the WSL side (no 9P/virtiofs bridge involved
    at all), which is what actually fixes the cross-boundary consistency
    bugs write_wsl_text's own docstring documents - those were specific
    to crossing that bridge, and SFTP never crosses it.
    """
    last_exc = None
    for attempt in range(_WSL_RETRY_ATTEMPTS + 1):
        try:
            sftp = _get_sftp_client()
            with sftp.open(wsl_target_path, "w") as f:
                f.write(content)
            return
        except FileNotFoundError as e:
            # A genuine error (e.g. the parent directory doesn't exist) -
            # not a connection problem, so raise immediately rather than
            # burning retries on something retrying can't fix.
            raise RuntimeError(f"writing {wsl_target_path} via WSL failed: {e}") from e
        except (paramiko.SSHException, OSError) as e:
            last_exc = e
            # Graduated recovery (confirmed necessary 2026-08-07, real sweep
            # failure): an SFTP failure doesn't necessarily mean the
            # underlying SSH transport is dead - the remote sftp-server
            # subprocess a channel depends on can die on its own while the
            # transport it rides on stays perfectly healthy. Always forcing
            # a full reconnect (fresh wsl.exe IP lookup + new TCP/SSH
            # handshake) wastes retry budget on the expensive path when
            # just reopening an SFTP channel on the SAME transport would
            # often have worked. So: discard only the SFTP client on the
            # first failure (the next _get_sftp_client() call re-checks the
            # existing transport's own is_active() and reuses it if it
            # genuinely still is) - only discard the SSH client too, forcing
            # the expensive full reconnect, once the cheap retry ALSO fails.
            _discard_dead_sftp_client()
            if attempt >= 1:
                _discard_dead_ssh_client()
            if attempt < _WSL_RETRY_ATTEMPTS:
                time.sleep(_WSL_RETRY_DELAY_S)
    raise RuntimeError(f"writing {wsl_target_path} via SFTP failed after retries: {last_exc}")


def write_wsl_text(wsl_target_path, content):
    """Write `content` to wsl_target_path (a native WSL /path) - either via
    a WSL-native process (piped through stdin) or SFTP, per _WSL_TRANSPORT
    - instead of a Windows-side open()/pathlib write through
    \\\\wsl.localhost\\....

    Needed for any file a WSL-native command (blockMesh, etc.) reads
    shortly after it's written, in a case directory new enough this
    session that neither side has "warmed up" to it yet. Confirmed
    2026-08-03: a brand-new directory/file written from the Windows side
    isn't reliably durable to WSL at all (not a timing issue - reproduced
    even after a full WSL VM restart). The reverse direction (a WSL-native
    write not yet visible to a Windows-side read) is a real but much
    shorter-lived gap for already-established directories - except for a
    just-created directory, where it isn't short-lived either: even 5s of
    Windows-side os.path.exists() polling never saw a directory blockMesh
    itself had just successfully cd'd into. Routing the write through a
    WSL-native process sidesteps both directions for the initial case
    setup files (mesh dicts, etc.) - see also windows_path_to_wsl_mnt,
    used for copying template files the same way.
    """
    if _WSL_TRANSPORT == "ssh":
        return _write_wsl_text_ssh(wsl_target_path, content)
    return _write_wsl_text_subprocess(wsl_target_path, content)


def _read_wsl_text_subprocess(wsl_source_path):
    r = subprocess.run(["wsl", "-e", "bash", "-lc", f'cat "{wsl_source_path}"'],
                        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"reading {wsl_source_path} via WSL failed: {r.stderr}")
    return r.stdout


def _read_wsl_text_ssh(wsl_source_path):
    last_exc = None
    for attempt in range(_WSL_RETRY_ATTEMPTS + 1):
        try:
            sftp = _get_sftp_client()
            with sftp.open(wsl_source_path, "r") as f:
                return f.read().decode()
        except FileNotFoundError as e:
            raise RuntimeError(f"reading {wsl_source_path} via WSL failed: {e}") from e
        except (paramiko.SSHException, OSError) as e:
            last_exc = e
            # Graduated recovery - see _write_wsl_text_ssh's matching
            # comment for why this doesn't always force a full reconnect.
            _discard_dead_sftp_client()
            if attempt >= 1:
                _discard_dead_ssh_client()
            if attempt < _WSL_RETRY_ATTEMPTS:
                time.sleep(_WSL_RETRY_DELAY_S)
    raise RuntimeError(f"reading {wsl_source_path} via SFTP failed after retries: {last_exc}")


def read_wsl_text(wsl_source_path):
    """Read a file's content - either via a WSL-native process (cat) or
    SFTP, per _WSL_TRANSPORT - instead of a Windows-side open()/pathlib
    read through \\\\wsl.localhost\\... - see write_wsl_text's docstring.
    The same cross-boundary gap can affect reads too, not just writes - a
    file a WSL-native command (mapFields, simpleFoam, ...) has just
    written/touched in a case directory that's brand new this session
    isn't reliably visible to a Windows-side read either.
    """
    if _WSL_TRANSPORT == "ssh":
        return _read_wsl_text_ssh(wsl_source_path)
    return _read_wsl_text_subprocess(wsl_source_path)


def read_case_file(case_dir, relative_path):
    """Read <case_dir>/<relative_path>, choosing the read mechanism
    automatically - see write_case_file (the write-side counterpart)."""
    case_dir_wsl = wsl_path(case_dir)
    if case_dir_wsl != case_dir:
        return read_wsl_text(f"{case_dir_wsl}/{relative_path}")
    with open(f"{case_dir}/{relative_path}") as f:
        return f.read()


def ensure_case_subdir(case_dir, relative_dir):
    """mkdir -p <case_dir>/<relative_dir>, choosing WSL-native vs plain
    Windows-side pathlib automatically - see write_case_file."""
    case_dir_wsl = wsl_path(case_dir)
    if case_dir_wsl != case_dir:
        run_wsl_or_raise(f'mkdir -p "{case_dir_wsl}/{relative_dir}"', "/", f"mkdir {relative_dir}")
    else:
        Path(f"{case_dir}/{relative_dir}").mkdir(parents=True, exist_ok=True)


def write_case_file(case_dir, relative_path, content):
    """Write `content` to <case_dir>/<relative_path>, choosing the write
    mechanism automatically: WSL-native (write_wsl_text) if case_dir is a
    real \\\\wsl.localhost\\... path - see write_wsl_text's docstring for
    why that matters for any file in a case directory that's brand new
    this session, no matter how much WSL-native activity (blockMesh, etc.)
    has already happened elsewhere in the same directory: confirmed
    2026-08-03 that "directory warmth" doesn't help, only a WSL-native
    write of THAT SPECIFIC file does - a Windows-side write into a
    never-before-written filename still failed even after 2 full minutes
    and several prior successful WSL-native commands in the same
    directory. Falls back to a plain Windows-side write for non-WSL paths
    (e.g. test fixtures using local temp dirs), where no cross-boundary
    handoff exists at all.
    """
    case_dir_wsl = wsl_path(case_dir)
    if case_dir_wsl != case_dir:
        write_wsl_text(f"{case_dir_wsl}/{relative_path}", content)
    else:
        with open(f"{case_dir}/{relative_path}", "w") as f:
            f.write(content)


def _run_wsl_subprocess(cmd, cwd_wsl):
    full_cmd = f'source {OPENFOAM_BASHRC} 2>/dev/null; cd "{cwd_wsl}" && {cmd}'
    for attempt in range(_WSL_RETRY_ATTEMPTS + 1):
        r = subprocess.run(["wsl", "-e", "bash", "-lc", full_cmd], capture_output=True, text=True)
        if not _looks_like_wsl_launch_failure(r.returncode, r.stdout, r.stderr):
            return r
        if attempt < _WSL_RETRY_ATTEMPTS:
            time.sleep(_WSL_RETRY_DELAY_S)
    return r


def _discard_dead_ssh_client():
    with _ssh_client_lock:
        client = _ssh_client_cache["client"]
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            _ssh_client_cache["client"] = None


def _run_wsl_ssh(cmd, cwd_wsl):
    """SSH-transport equivalent of _run_wsl_subprocess - same full_cmd
    shape, same retry count, but retries are for a dead/dropped SSH
    connection (WSL restart, sleep/wake), not "wsl.exe failed to launch"
    (there's no wsl.exe spawn per call anymore - see _get_ssh_client's own
    docstring). A genuine remote command failure (a real, non-zero exit
    with real stdout/stderr) is returned immediately, never retried -
    same intent as the subprocess path's _looks_like_wsl_launch_failure
    check, just detected differently (an exception from the SSH layer
    itself, rather than an empty-output heuristic).
    """
    full_cmd = f'source {OPENFOAM_BASHRC} 2>/dev/null; cd "{cwd_wsl}" && {cmd}'
    last_exc = None
    for attempt in range(_WSL_RETRY_ATTEMPTS + 1):
        try:
            client = _get_ssh_client()
            with _ssh_channel_semaphore:
                _, stdout, stderr = client.exec_command(full_cmd)
                out = stdout.read().decode(errors="replace")
                err = stderr.read().decode(errors="replace")
                returncode = stdout.channel.recv_exit_status()
            return subprocess.CompletedProcess(["ssh", full_cmd], returncode, out, err)
        except (paramiko.SSHException, OSError) as e:
            last_exc = e
            _discard_dead_ssh_client()
            if attempt < _WSL_RETRY_ATTEMPTS:
                time.sleep(_WSL_RETRY_DELAY_S)
    raise RuntimeError(f"SSH connection to WSL failed after {_WSL_RETRY_ATTEMPTS + 1} attempts: {last_exc}")


def run_wsl(cmd, cwd_wsl):
    if _WSL_TRANSPORT == "ssh":
        return _run_wsl_ssh(cmd, cwd_wsl)
    return _run_wsl_subprocess(cmd, cwd_wsl)


def run_wsl_or_raise(cmd, cwd_wsl, step_name):
    r = run_wsl(cmd, cwd_wsl)
    if r.returncode != 0:
        raise RuntimeError(f"{step_name} failed (exit {r.returncode}):\n{r.stdout}\n{r.stderr}")
    return r


class StoppedByUser(Exception):
    """Raised when a caller's should_stop() callback returns True during a
    run_wsl_streaming() call - lets the GUI distinguish a deliberate stop
    from a genuine failure."""


def _signal_wsl_in_dir(cwd_wsl, name_pattern, sig):
    """Send signal `sig` (e.g. "9", "STOP", "CONT") to processes matching
    name_pattern whose cwd is exactly cwd_wsl - a bare `pkill -f
    name_pattern` would hit every same-named solver process system-wide,
    which is fine when only one solve ever runs at a time but wrong once
    several ACH/Z combinations' solvers (all named e.g. "pimpleFoam") can
    be running concurrently: one combination stalling, being stopped, or
    being paused must not affect its siblings.
    """
    script = (
        f"target=$(readlink -f '{cwd_wsl}'); "
        f"for p in $(pgrep -f '{name_pattern}'); do "
        f"[ \"$(readlink -f /proc/$p/cwd 2>/dev/null)\" = \"$target\" ] && kill -{sig} $p; "
        f"done"
    )
    # Routed through run_wsl (transport-aware: SSH when enabled) rather
    # than a direct subprocess.run - same kill/pause targeting either way
    # (still name+cwd matching, deliberately NOT simplified to a captured
    # PID even under the SSH transport: cmd strings like "pimpleFoam 2>&1
    # | tee log.pimpleFoam" mean $! after backgrounding only captures the
    # LAST pipeline stage (tee), not the actual solver - killing that
    # wouldn't reliably stop the solver itself. Name+cwd matching finds
    # the real process directly, pipe or no pipe.
    try:
        run_wsl(script, "/")
    except Exception:
        pass


def _kill_wsl_in_dir(cwd_wsl, name_pattern):
    _signal_wsl_in_dir(cwd_wsl, name_pattern, "9")


def _run_wsl_streaming_subprocess(cmd, cwd_wsl, on_line=None, should_stop=None, kill_pattern=None,
                                   stall_timeout=_DEFAULT_STALL_TIMEOUT_S, should_pause=None):
    """Like run_wsl, but streams stdout line-by-line to on_line(line) as
    it's produced instead of only returning once the whole command exits -
    this is what lets the GUI show live solver progress (e.g. "Time = N"
    lines) instead of a silent wait followed by a dump at the end.

    should_stop, if given, is checked (at least) every _STALL_POLL_INTERVAL_S
    seconds, whether or not new output has arrived - if it returns True,
    the WSL-side process is killed (by name, via kill_pattern - matching
    the Windows-side wsl.exe wrapper's own process doesn't reliably kill
    the process running inside WSL) and the command is abandoned. Returns
    a CompletedProcess-like object either way, with .returncode/.stdout
    covering everything captured so far - callers check should_stop()
    themselves afterward to distinguish a deliberate stop from a crash.

    should_pause, if given, is polled the same way - but instead of
    killing the process, it's suspended in place (kill -STOP, via
    kill_pattern) and the call blocks until should_pause() goes False
    again (kill -CONT) or should_stop() fires. This is a genuine pause -
    zero iterations lost, no chunk boundary needed, and critically no
    exception is ever raised, unlike a Stop - a caller further up (see
    scenario_runs.run_sweep/run_decay_sweep's own ach_worker) cleans up
    shared per-ACH state in a finally block keyed off StoppedByUser propagating, and pausing
    must never trigger that.

    stall_timeout: give up (kill the process, same as a Stop) if NO new
    output arrives for this long. Reads happen on a background thread into
    a queue, polled with a timeout, rather than the previous plain
    `for line in proc.stdout` loop - that blocks indefinitely on a dead
    pipe, and critically, should_stop() was only ever checked *after* a
    new line arrived, so with zero output it never ran at all, not even
    to notice a Stop request. Confirmed directly: a real scenario sweep's
    concurrent decay runs both died (something killed them externally,
    mid-iteration) and the sweep then sat completely silent - no error,
    nothing written, Stop wouldn't have done anything either - for over an
    hour with no way to notice. A stalled process now surfaces as a
    non-zero returncode (terminated, same path a normal crash already
    takes), not a silent hang.
    """
    full_cmd = f'source {OPENFOAM_BASHRC} 2>/dev/null; cd "{cwd_wsl}" && {cmd}'

    for attempt in range(_WSL_RETRY_ATTEMPTS + 1):
        proc = subprocess.Popen(
            ["wsl", "-e", "bash", "-lc", full_cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        line_queue = queue.Queue()

        def _reader(pipe=proc.stdout, q=line_queue):
            for raw_line in iter(pipe.readline, ""):
                q.put(raw_line.rstrip("\n"))
            q.put(None)  # sentinel: pipe closed, process has exited

        threading.Thread(target=_reader, daemon=True).start()

        lines = []
        stopped = False
        last_output_time = time.time()
        while True:
            try:
                line = line_queue.get(timeout=_STALL_POLL_INTERVAL_S)
            except queue.Empty:
                line = "__timeout__"

            if line is None:
                break  # process exited on its own, nothing more to read

            if line != "__timeout__":
                last_output_time = time.time()
                lines.append(line)
                if on_line:
                    on_line(line)

            if should_stop is not None and should_stop():
                stopped = True
                if kill_pattern:
                    _kill_wsl_in_dir(cwd_wsl, kill_pattern)
                proc.terminate()
                break
            if should_pause is not None and should_pause():
                if kill_pattern:
                    _signal_wsl_in_dir(cwd_wsl, kill_pattern, "STOP")
                if on_line:
                    on_line("[paused - process suspended, waiting to continue]")
                while should_pause() and not (should_stop is not None and should_stop()):
                    time.sleep(_STALL_POLL_INTERVAL_S)
                if should_stop is not None and should_stop():
                    stopped = True
                    if kill_pattern:
                        _kill_wsl_in_dir(cwd_wsl, kill_pattern)
                    proc.terminate()
                    break
                if kill_pattern:
                    _signal_wsl_in_dir(cwd_wsl, kill_pattern, "CONT")
                if on_line:
                    on_line("[resumed]")
                last_output_time = time.time()  # don't count pause duration as a stall
                continue
            if time.time() - last_output_time > stall_timeout:
                if on_line:
                    on_line(f"[no output for {stall_timeout}s - the process looks stuck/"
                            f"unresponsive (or was killed externally); giving up on it]")
                if kill_pattern:
                    _kill_wsl_in_dir(cwd_wsl, kill_pattern)
                proc.terminate()
                break

        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)

        if stopped or not _looks_like_wsl_launch_failure(proc.returncode, "\n".join(lines), ""):
            return subprocess.CompletedProcess(proc.args, proc.returncode, "\n".join(lines), "")
        if attempt < _WSL_RETRY_ATTEMPTS:
            if on_line:
                on_line(f"[wsl launch produced no output - retrying ({attempt + 1}/{_WSL_RETRY_ATTEMPTS})...]")
            time.sleep(_WSL_RETRY_DELAY_S)

    return subprocess.CompletedProcess(proc.args, proc.returncode, "\n".join(lines), "")


def _run_wsl_streaming_ssh(cmd, cwd_wsl, on_line=None, should_stop=None, kill_pattern=None,
                            stall_timeout=_DEFAULT_STALL_TIMEOUT_S, should_pause=None):
    """SSH-transport equivalent of _run_wsl_streaming_subprocess - same
    line-by-line callback/stall-timeout/should_stop/should_pause contract,
    read from a persistent paramiko Channel instead of a fresh wsl.exe
    subprocess's pipe.

    Deliberately keeps kill/pause targeting UNCHANGED (still
    _kill_wsl_in_dir/_signal_wsl_in_dir's name+cwd pattern match, not a
    captured PID) - cmd strings here are routinely a pipeline (e.g.
    "pimpleFoam 2>&1 | tee log.pimpleFoam"), and capturing $! after
    backgrounding only gives the LAST pipeline stage's PID (tee), not the
    actual solver's - killing that wouldn't reliably stop the solver
    itself. Name+cwd matching finds the real process directly regardless.
    _kill_wsl_in_dir/_signal_wsl_in_dir already route through run_wsl, so
    they're transport-aware without any further change here.

    Channel.recv() is NOT line-buffered the way a subprocess pipe's
    readline() is - this does its own buffering/newline-splitting in the
    reader thread instead.
    """
    full_cmd = f'source {OPENFOAM_BASHRC} 2>/dev/null; cd "{cwd_wsl}" && {cmd}'
    channel = None
    lines = []
    returncode = -1

    for attempt in range(_WSL_RETRY_ATTEMPTS + 1):
        # Held for this whole attempt's channel lifetime (released just
        # before returning/retrying below, or immediately here on a
        # failure to even open the channel) - see _ssh_channel_semaphore's
        # own comment.
        _ssh_channel_semaphore.acquire()
        try:
            client = _get_ssh_client()
            channel = client.get_transport().open_session()
            channel.set_combine_stderr(True)
            channel.exec_command(full_cmd)
        except (paramiko.SSHException, OSError) as e:
            _ssh_channel_semaphore.release()
            _discard_dead_ssh_client()
            if attempt < _WSL_RETRY_ATTEMPTS:
                if on_line:
                    on_line(f"[SSH connection to WSL failed - retrying ({attempt + 1}/{_WSL_RETRY_ATTEMPTS})...]")
                time.sleep(_WSL_RETRY_DELAY_S)
                continue
            raise RuntimeError(f"Could not start streaming command over SSH: {e}") from e

        line_queue = queue.Queue()

        def _reader(ch=channel, q=line_queue):
            buf = b""
            while True:
                try:
                    chunk = ch.recv(4096)
                except Exception:
                    chunk = b""
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    q.put(raw_line.decode(errors="replace"))
            if buf:
                q.put(buf.decode(errors="replace"))
            q.put(None)  # sentinel: channel closed, process has exited

        threading.Thread(target=_reader, daemon=True).start()

        lines = []
        stopped = False
        last_output_time = time.time()
        while True:
            try:
                line = line_queue.get(timeout=_STALL_POLL_INTERVAL_S)
            except queue.Empty:
                line = "__timeout__"

            if line is None:
                break  # channel closed on its own, nothing more to read

            if line != "__timeout__":
                last_output_time = time.time()
                lines.append(line)
                if on_line:
                    on_line(line)

            if should_stop is not None and should_stop():
                stopped = True
                if kill_pattern:
                    _kill_wsl_in_dir(cwd_wsl, kill_pattern)
                break
            if should_pause is not None and should_pause():
                if kill_pattern:
                    _signal_wsl_in_dir(cwd_wsl, kill_pattern, "STOP")
                if on_line:
                    on_line("[paused - process suspended, waiting to continue]")
                while should_pause() and not (should_stop is not None and should_stop()):
                    time.sleep(_STALL_POLL_INTERVAL_S)
                if should_stop is not None and should_stop():
                    stopped = True
                    if kill_pattern:
                        _kill_wsl_in_dir(cwd_wsl, kill_pattern)
                    break
                if kill_pattern:
                    _signal_wsl_in_dir(cwd_wsl, kill_pattern, "CONT")
                if on_line:
                    on_line("[resumed]")
                last_output_time = time.time()  # don't count pause duration as a stall
                continue
            if time.time() - last_output_time > stall_timeout:
                if on_line:
                    on_line(f"[no output for {stall_timeout}s - the process looks stuck/"
                            f"unresponsive (or was killed externally); giving up on it]")
                if kill_pattern:
                    _kill_wsl_in_dir(cwd_wsl, kill_pattern)
                break

        # Grace period for the real exit status, same "wait, then force"
        # shape as the subprocess path's proc.wait(timeout=15)/proc.kill()
        # - there's no local process to force-kill here (kill_pattern
        # already asked the REMOTE process to exit above, if this loop
        # ended via stop/stall), only the channel itself to give up on.
        # Skipped entirely on a natural EOF (line is None above) - the
        # channel's exit status is already ready by then, same as the
        # subprocess path's clean-exit case never calling proc.kill().
        if not channel.exit_status_ready():
            deadline = time.time() + _EXIT_STATUS_GRACE_S
            while not channel.exit_status_ready() and time.time() < deadline:
                time.sleep(0.2)
            if not channel.exit_status_ready():
                channel.close()
        try:
            returncode = channel.recv_exit_status() if channel.exit_status_ready() else -1
        except Exception:
            returncode = -1
        finally:
            _ssh_channel_semaphore.release()

        combined_output = "\n".join(lines)
        if stopped or not _looks_like_wsl_launch_failure(returncode, combined_output, ""):
            return subprocess.CompletedProcess(full_cmd, returncode, combined_output, "")
        if attempt < _WSL_RETRY_ATTEMPTS:
            if on_line:
                on_line(f"[connection produced no output - retrying ({attempt + 1}/{_WSL_RETRY_ATTEMPTS})...]")
            time.sleep(_WSL_RETRY_DELAY_S)

    return subprocess.CompletedProcess(full_cmd, returncode, "\n".join(lines), "")


def run_wsl_streaming(cmd, cwd_wsl, on_line=None, should_stop=None, kill_pattern=None,
                       stall_timeout=_DEFAULT_STALL_TIMEOUT_S, should_pause=None):
    """Streams a long-running command's output line-by-line to on_line(),
    via either a fresh wsl.exe subprocess or a persistent SSH channel, per
    _WSL_TRANSPORT - see _run_wsl_streaming_subprocess/_run_wsl_streaming_ssh
    for the full contract (identical between the two)."""
    if _WSL_TRANSPORT == "ssh":
        return _run_wsl_streaming_ssh(cmd, cwd_wsl, on_line=on_line, should_stop=should_stop,
                                       kill_pattern=kill_pattern, stall_timeout=stall_timeout,
                                       should_pause=should_pause)
    return _run_wsl_streaming_subprocess(cmd, cwd_wsl, on_line=on_line, should_stop=should_stop,
                                          kill_pattern=kill_pattern, stall_timeout=stall_timeout,
                                          should_pause=should_pause)
