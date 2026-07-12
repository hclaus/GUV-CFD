"""Shared helpers for shelling out to WSL/OpenFOAM binaries, used by both
run_pipeline.py (decay scenario) and steady_state_pipeline.py (continuous
source scenario).
"""
import subprocess
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
    full_cmd = f"source {OPENFOAM_BASHRC} 2>/dev/null; cd {cwd_wsl} && {cmd}"
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


def run_wsl_streaming(cmd, cwd_wsl, on_line=None, should_stop=None, kill_pattern=None):
    """Like run_wsl, but streams stdout line-by-line to on_line(line) as
    it's produced instead of only returning once the whole command exits -
    this is what lets the GUI show live solver progress (e.g. "Time = N"
    lines) instead of a silent wait followed by a dump at the end.

    should_stop, if given, is checked after every line; if it returns True,
    the WSL-side process is killed (by name, via kill_pattern - matching
    the Windows-side wsl.exe wrapper's own process doesn't reliably kill
    the process running inside WSL) and the command is abandoned. Returns
    a CompletedProcess-like object either way, with .returncode/.stdout
    covering everything captured so far - callers check should_stop()
    themselves afterward to distinguish a deliberate stop from a crash.
    """
    full_cmd = f"source {OPENFOAM_BASHRC} 2>/dev/null; cd {cwd_wsl} && {cmd}"

    for attempt in range(_WSL_RETRY_ATTEMPTS + 1):
        proc = subprocess.Popen(
            ["wsl", "-e", "bash", "-lc", full_cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        lines = []
        stopped = False
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            if on_line:
                on_line(line)
            if should_stop is not None and should_stop():
                stopped = True
                if kill_pattern:
                    subprocess.run(
                        ["wsl", "-e", "bash", "-lc", f"pkill -9 -f '{kill_pattern}'"],
                        capture_output=True,
                    )
                proc.terminate()
                break
        proc.wait(timeout=15)

        if stopped or not _looks_like_wsl_launch_failure(proc.returncode, "\n".join(lines), ""):
            return subprocess.CompletedProcess(proc.args, proc.returncode, "\n".join(lines), "")
        if attempt < _WSL_RETRY_ATTEMPTS:
            if on_line:
                on_line(f"[wsl launch produced no output - retrying ({attempt + 1}/{_WSL_RETRY_ATTEMPTS})...]")
            time.sleep(_WSL_RETRY_DELAY_S)

    return subprocess.CompletedProcess(proc.args, proc.returncode, "\n".join(lines), "")
