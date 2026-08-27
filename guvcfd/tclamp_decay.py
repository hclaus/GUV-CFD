"""Custom OpenFOAM function object (TClampDecay): a real-time correction
for Phase 2's own outer-iteration UV-sink divergence mechanism (see
ANALYSIS_LOG.md) - at very high local kUV, a single cell's T can blow up
past 1e100 (or go negative) within ~20 iterations, even with adaptive
T-relaxation already at its floor (splice.compute_adaptive_scalar_relaxation).

Rather than a hard reset to a boundary value, an out-of-[0,Tmax] cell is
replaced by T*exp(-kUV*dt) (then clamped into [0,Tmax] to guarantee
boundedness regardless) - the pure-sink ODE dT/dt=-kUV*T's own analytic
solution, so the correction stays physically motivated instead of an
arbitrary snap. See guvcfd/openfoam_functionobjects/TClampDecay/ for the
actual C++ implementation this module compiles and wires in.

Tmax itself is set per-run as a multiplier (t-clamp-decay-multiplier,
default 1.3) times Phase 1's own converged source-zone max T
(source_zone_max_T below) - the only physically meaningful reference this
pipeline has for "how concentrated does the source actually get": no
physical mechanism lets a passive, non-created-elsewhere scalar exceed its
own source's peak value.
"""
from pathlib import Path

import numpy as np

from .contaminant_source import _source_box
from .case_io import read_cell_centers, read_openfoam_scalar_field
from .splice import splice_after_named_entry
from .wsl_utils import read_case_file, run_wsl, run_wsl_or_raise, windows_path_to_wsl_mnt

_LIB_NAME = "libTClampDecayFunctionObject.so"
_USER_SRC_DIR = "$WM_PROJECT_USER_DIR/src/TClampDecay"
_PACKAGE_SRC_DIR = Path(__file__).resolve().parent / "openfoam_functionobjects" / "TClampDecay"
_FUNCTION_NAME = "TClampDecay1"


def source_zone_max_T(case_dir, source_center, source_size, cell_size=None, room_dims=None,
                       snapshot_path="phase1_T.snapshot", time_dir="0"):
    """Max T within the source/injection zone's own cells, read from
    `snapshot_path` (default: Phase 1's converged T field, preserved as a
    plain file by run_steady_state_scenario right before Phase 2 starts -
    see its own "saving Phase 1's final T field" comment). Filters cell
    centers (already written to <time_dir>/{Cx,Cy,Cz} by setup_case) to
    the same outward-snapped box contaminant_source carves the source
    cellZone from, so this always matches the ACTUAL carved zone, not the
    raw nominal center/size.
    """
    centers = read_cell_centers(case_dir, time_dir)
    lo, hi = _source_box(source_center, source_size, cell_size=cell_size, room_dims=room_dims)
    lo, hi = np.array(lo), np.array(hi)
    mask = np.all((centers >= lo) & (centers <= hi), axis=1)
    if not mask.any():
        raise RuntimeError(f"source_zone_max_T: no cells found inside the source box "
                            f"{lo.tolist()}-{hi.tolist()} in {case_dir}")
    t_values = np.array(read_openfoam_scalar_field(f"{case_dir}/{snapshot_path}"))
    if len(t_values) != len(centers):
        raise RuntimeError(f"source_zone_max_T: {snapshot_path} has {len(t_values)} cells but "
                            f"{time_dir}/Cx has {len(centers)} - mesh/field mismatch")
    return float(t_values[mask].max())


def ensure_tclamp_decay_compiled(log_fn=None):
    """Idempotently compile the TClampDecay function object library inside
    WSL's OpenFOAM user source tree. The source is fixed and version-
    controlled here (guvcfd/openfoam_functionobjects/TClampDecay/), not
    per-project, so this only actually does anything once per WSL
    environment - every later call (including every later run on this
    machine) is a cheap no-op check.
    """
    log_fn = log_fn or (lambda m: None)
    check = run_wsl(f'test -f "$FOAM_USER_LIBBIN/{_LIB_NAME}" && echo YES || echo NO',
                     "$WM_PROJECT_USER_DIR")
    if check.stdout.strip() == "YES":
        return
    log_fn("Compiling the TClampDecay OpenFOAM function object (first use on this machine)...")
    pkg_src_mnt = windows_path_to_wsl_mnt(str(_PACKAGE_SRC_DIR))
    run_wsl_or_raise(
        f'mkdir -p "{_USER_SRC_DIR}/Make" && '
        f'cp "{pkg_src_mnt}/TClampDecay.H" "{_USER_SRC_DIR}/" && '
        f'cp "{pkg_src_mnt}/TClampDecay.C" "{_USER_SRC_DIR}/" && '
        f'cp "{pkg_src_mnt}/Make/files" "{_USER_SRC_DIR}/Make/" && '
        f'cp "{pkg_src_mnt}/Make/options" "{_USER_SRC_DIR}/Make/"',
        "$WM_PROJECT_USER_DIR", "copying TClampDecay source into the WSL OpenFOAM user tree",
    )
    run_wsl_or_raise(f'wmake libso "{_USER_SRC_DIR}"', "$WM_PROJECT_USER_DIR",
                      "compiling TClampDecay (wmake libso)")
    recheck = run_wsl(f'test -f "$FOAM_USER_LIBBIN/{_LIB_NAME}" && echo YES || echo NO',
                       "$WM_PROJECT_USER_DIR")
    if recheck.stdout.strip() != "YES":
        raise RuntimeError("TClampDecay compiled without a wmake error but the expected "
                            f"library ($FOAM_USER_LIBBIN/{_LIB_NAME}) is still missing.")
    log_fn("TClampDecay compiled.")


def tclamp_decay_function_object(Tmax, field="T", indent="    "):
    """Splice-ready controlDict functions{} entry - see
    splice.splice_into_functions_block(). Tmax formatted with %.6g so an
    extreme or float-noisy value never produces something OpenFOAM's
    dictionary parser can't read back as a plain scalar.
    """
    lines = [
        f"{_FUNCTION_NAME}", "{",
        "    type            TClampDecay;",
        f'    libs            ("{_LIB_NAME}");',
        f"    field           {field};",
        f"    Tmax            {Tmax:.6g};",
        "    executeControl  timeStep;",
        "    executeInterval 1;",
        "    writeControl    none;",
        "}",
    ]
    return "\n".join(indent + line if line else "" for line in lines)


def splice_tclamp_decay_if_needed(case_dir, Tmax, field="T"):
    """Idempotently splice tclamp_decay_function_object() into case_dir's
    controlDict - a no-op if it's already there (matches
    monitoring.splice_live_vol_average_if_needed's own idempotency
    pattern), so this is safe to call on every Phase 2 invocation
    (fresh start or a resumed/chunked continuation alike).

    Inserted right after scalarTransport1 (not at the end of functions{})
    so the correction runs BEFORE any volAverage-style room/patch/zone
    tracking reads T that same timestep - otherwise a room-average could
    momentarily include an out-of-[0,Tmax] cell that hasn't been
    corrected yet, biasing any average computed from it (see
    ANALYSIS_LOG.md).
    """
    content = read_case_file(case_dir, "system/controlDict")
    if _FUNCTION_NAME in content:
        return
    block = tclamp_decay_function_object(Tmax, field=field)
    _, n_open, n_close = splice_after_named_entry(case_dir, "scalarTransport1", block)
    if n_open != n_close:
        raise RuntimeError(f"Brace mismatch after TClampDecay splice: open={n_open} close={n_close}")
