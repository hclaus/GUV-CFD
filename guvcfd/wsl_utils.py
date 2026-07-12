"""Shared helpers for shelling out to WSL/OpenFOAM binaries, used by both
run_pipeline.py (decay scenario) and steady_state_pipeline.py (continuous
source scenario).
"""
import subprocess

OPENFOAM_BASHRC = "/usr/lib/openfoam/openfoam2412/etc/bashrc"


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
    return subprocess.run(["wsl", "-e", "bash", "-lc", full_cmd], capture_output=True, text=True)


def run_wsl_or_raise(cmd, cwd_wsl, step_name):
    r = run_wsl(cmd, cwd_wsl)
    if r.returncode != 0:
        raise RuntimeError(f"{step_name} failed (exit {r.returncode}):\n{r.stdout}\n{r.stderr}")
    return r
