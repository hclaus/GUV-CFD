"""Shared helpers for shelling out to WSL/OpenFOAM binaries, used by both
run_pipeline.py (decay scenario) and steady_state_pipeline.py (continuous
source scenario).
"""
import queue
import subprocess
import threading
import time

OPENFOAM_BASHRC = "/usr/lib/openfoam/openfoam2412/etc/bashrc"

# Observed repeatedly in practice: wsl.exe occasionally fails to launch/
# attach to the WSL side at all, returning a non-zero exit with *nothing*
# captured on either stdout or stderr - not a real command failure (every
# command actually reaching a shell inside WSL, success or failure, prints
# something). A couple of quick retries clears it; genuine failures always
# have real output and are never masked by this.
_WSL_RETRY_ATTEMPTS = 2
_WSL_RETRY_DELAY_S = 1.5

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


def run_wsl(cmd, cwd_wsl):
    full_cmd = f'source {OPENFOAM_BASHRC} 2>/dev/null; cd "{cwd_wsl}" && {cmd}'
    for attempt in range(_WSL_RETRY_ATTEMPTS + 1):
        r = subprocess.run(["wsl", "-e", "bash", "-lc", full_cmd], capture_output=True, text=True)
        if not _looks_like_wsl_launch_failure(r.returncode, r.stdout, r.stderr):
            return r
        if attempt < _WSL_RETRY_ATTEMPTS:
            time.sleep(_WSL_RETRY_DELAY_S)
    return r


def run_wsl_or_raise(cmd, cwd_wsl, step_name):
    r = run_wsl(cmd, cwd_wsl)
    if r.returncode != 0:
        raise RuntimeError(f"{step_name} failed (exit {r.returncode}):\n{r.stdout}\n{r.stderr}")
    return r


class StoppedByUser(Exception):
    """Raised when a caller's should_stop() callback returns True during a
    run_wsl_streaming() call - lets the GUI distinguish a deliberate stop
    from a genuine failure."""


def _kill_wsl_pattern(kill_pattern):
    subprocess.run(["wsl", "-e", "bash", "-lc", f"pkill -9 -f '{kill_pattern}'"], capture_output=True)


def run_wsl_streaming(cmd, cwd_wsl, on_line=None, should_stop=None, kill_pattern=None,
                       stall_timeout=_DEFAULT_STALL_TIMEOUT_S):
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
                    _kill_wsl_pattern(kill_pattern)
                proc.terminate()
                break
            if time.time() - last_output_time > stall_timeout:
                if on_line:
                    on_line(f"[no output for {stall_timeout}s - the process looks stuck/"
                            f"unresponsive (or was killed externally); giving up on it]")
                if kill_pattern:
                    _kill_wsl_pattern(kill_pattern)
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
