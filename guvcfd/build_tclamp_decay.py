"""Standalone entry point to pre-compile the TClampDecay OpenFOAM function
object (see tclamp_decay.py) before first use, instead of letting it compile
silently the first time a run enables "Use T divergence clamp".

Run via: uv run python -m guvcfd.build_tclamp_decay
(or double-click BuildTClampDecay.bat at the repo root, which wraps this).

Nothing else in the app requires this to be run manually - a normal run
calls tclamp_decay.ensure_tclamp_decay_compiled() itself, and it's a cheap
no-op check every time after the first successful build. This script exists
so a new install (or a new machine) can verify the WSL/OpenFOAM/compiler
toolchain up front, with clear pass/fail output, rather than finding out
in the middle of an unattended overnight sweep.
"""
import sys

from .tclamp_decay import ensure_tclamp_decay_compiled
from .wsl_utils import run_wsl


def main():
    print("Checking WSL is reachable...")
    r = run_wsl("echo ok", "/")
    if r.returncode != 0 or "ok" not in r.stdout:
        print("ERROR: could not reach WSL. Is a WSL distro installed and running?")
        print(f"  stdout: {r.stdout!r}")
        print(f"  stderr: {r.stderr!r}")
        sys.exit(1)
    print("WSL OK.")

    print("Checking the OpenFOAM environment (g++, wmake) is available...")
    r = run_wsl("command -v g++ && command -v wmake", "/")
    if r.returncode != 0:
        print("ERROR: g++ and/or wmake not found inside WSL after sourcing OpenFOAM's bashrc.")
        print("  This normally comes bundled with 'openfoam2412-default' (see \"Linux installation.md\", "
              "section 1e) - if you installed a minimal/runtime-only OpenFOAM package instead, install "
              "build tools directly:")
        print("    sudo apt-get install -y build-essential")
        sys.exit(1)
    print("g++ and wmake OK.")

    print("Compiling TClampDecay (a no-op if already built)...")
    try:
        ensure_tclamp_decay_compiled(log_fn=print)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    print("Done - TClampDecay is ready. \"Use T divergence clamp\" can now be enabled in Settings.")


if __name__ == "__main__":
    main()
