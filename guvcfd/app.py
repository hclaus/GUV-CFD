"""GUV-CFD GUI: load a .guv project, configure inlet/outlet/fan and the
scenario type, preview the 3D case setup live, and (eventually) run the
pipeline. Local single-user tool - run `python -m guvcfd.app` and open
the printed localhost URL.
"""
import json
import math
import re
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

import dash
import dash_bootstrap_components as dbc
import plotly.graph_objs as go
from dash import Input, Output, State, dcc, html
from guv_calcs import Project

from .decay_analysis import write_results_summary
from .fan import fan_fvoptions_entry
from .initial_fields import compute_inlet_velocity
from .run_pipeline import setup_case
from .splice import set_control_dict_start_from, set_control_dict_time
from .steady_state_pipeline import run_steady_state_scenario
from .visualization import plot_case
from .wsl_utils import run_wsl, run_wsl_or_raise, run_wsl_streaming, wsl_path, StoppedByUser

# Reference case setup_case() copies its static config (controlDict,
# fvSchemes, fvSolution, transportProperties, turbulenceProperties,
# volAverageDict) from - a previously verified-working pimpleFoam/
# scalarTransportFoam case, bundled into the package itself (not a local
# user path) so the app is portable across machines/checkouts.
TEMPLATE_CASE_DIR = str(Path(__file__).parent / "templates" / "case_template")

# Single-user local tool - a plain module-level holder for the currently
# loaded project is simpler and more appropriate here than real session
# state (dcc.Store can't hold a Project object directly - not JSON-safe).
# settings_path is the currently open/saved .guvcfd file (None if unsaved).
_loaded = {"project": None, "room": None, "path": None, "settings_path": None}

WALL_OPTIONS = [{"label": w, "value": w} for w in ("xMin", "xMax")]

# Every plain-value form field that a GUV-CFD project file (.guvcfd, JSON)
# saves/restores. Position fields use their "-input" id, not "-slider" -
# the slider is kept in sync from it (see _register_position_field), so
# only the number box needs to round-trip.
SETTINGS_FIELDS = [
    "project-description", "case-dir", "ach", "z-value",
    "inlet-show", "inlet-wall", "inlet-y-input", "inlet-z-input", "inlet-size-w", "inlet-size-h",
    "outlet-show", "outlet-wall", "outlet-y-input", "outlet-z-input", "outlet-size-w", "outlet-size-h",
    "fan-enable", "fan-speed", "fan-direction", "fan-radius", "fan-thickness",
    "fan-x-input", "fan-y-input", "fan-z-input",
    "sim-type", "pimple-end-time", "pimple-write-interval",
    "target-t-ss", "inject-x-input", "inject-y-input", "inject-z-input",
    "phase1-iterations", "phase2-iterations",
]

# Position-field spec: (prefix, label, room-dimension attr for the slider's
# max, default-value function of room, initial default/min/max/step used
# before any project is loaded). Shared by inlet/outlet, fan, and injection
# controls so their slider<->number sync + "reset to room" callbacks can be
# registered in one loop instead of duplicated per field.
POSITION_FIELDS = [
    ("inlet-y", "Across-wall position — Y (m)", "y", lambda r: r.y / 2, 1.5, 0, 10, 0.05),
    ("inlet-z", "Height — Z (m)", "z", lambda r: 0.85 * r.z, 2.1, 0, 5, 0.05),
    ("outlet-y", "Across-wall position — Y (m)", "y", lambda r: r.y / 2, 1.5, 0, 10, 0.05),
    ("outlet-z", "Height — Z (m)", "z", lambda r: 0.15 * r.z, 0.4, 0, 5, 0.05),
    ("fan-x", "X position (m)", "x", lambda r: r.x / 2, 2.0, 0, 10, 0.05),
    ("fan-y", "Y position (m)", "y", lambda r: r.y / 2, 1.5, 0, 10, 0.05),
    ("fan-z", "Height — Z (m)", "z", lambda r: max(r.z - 0.3, 0), 2.2, 0, 5, 0.05),
    ("inject-x", "X position (m)", "x", lambda r: r.x / 2, 2.0, 0, 10, 0.05),
    ("inject-y", "Y position (m)", "y", lambda r: r.y / 2, 1.5, 0, 10, 0.05),
    ("inject-z", "Height — Z (m)", "z", lambda r: min(1.5, r.z), 1.5, 0, 5, 0.05),
]
_POSITION_FIELD_BY_PREFIX = {f[0]: f for f in POSITION_FIELDS}


def _compute_default_run_dir():
    """Ask WSL for OpenFOAM's own $FOAM_RUN convention and create it if
    missing, so the GUI's default project directory is a real, usable path
    rather than a guess. Returns a \\\\wsl.localhost\\... UNC path (browsable
    from Windows); wsl_utils.wsl_path() converts it back for subprocess use.
    """
    try:
        r = run_wsl('mkdir -p "$FOAM_RUN"; printf "%s|%s" "$WSL_DISTRO_NAME" "$FOAM_RUN"', "~")
        distro, _, run_path = r.stdout.strip().partition("|")
        if not run_path:
            return ""
        return "\\\\wsl.localhost\\" + distro + run_path.replace("/", "\\")
    except Exception:
        return ""


_DEFAULT_RUN_DIR = _compute_default_run_dir()

_UNSAFE_FOLDER_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_folder_name(name):
    name = _UNSAFE_FOLDER_CHARS_RE.sub("_", name).strip("_")
    return name or "case"


def _fresh_case_dir(guv_path):
    """A new, never-colliding project-directory default under $FOAM_RUN,
    named after the loaded .guv file. Always points at a subfolder (never
    $FOAM_RUN itself, which every project would otherwise dump straight
    into) and always a folder that doesn't exist yet - loading the same
    project twice gets "name", "name-2", "name-3", ... rather than one run
    silently overwriting another's.
    """
    if not _DEFAULT_RUN_DIR:
        return _DEFAULT_RUN_DIR
    base_name = _sanitize_folder_name(Path(guv_path).stem if guv_path else "case")
    candidate = f"{_DEFAULT_RUN_DIR}\\{base_name}"
    n = 2
    while Path(candidate).exists():
        candidate = f"{_DEFAULT_RUN_DIR}\\{base_name}-{n}"
        n += 1
    return candidate

# Background-thread run state - a real pipeline run takes minutes, far too
# long for a single Dash callback/HTTP request, so it runs in a daemon
# thread while a dcc.Interval polls this dict for the GUI to display.
_run_state = {
    "status": "idle", "log": [], "case_dir": None, "sim_type": None,
    "steps": [], "step_status": {}, "markers": [],
    "current_time": None, "start_time": None, "stop_requested": False,
}

# Checklist shown on the Processing tab, and the log-line substrings that
# advance it - reuses the log_fn messages the pipeline already emits rather
# than threading a separate step-tracking callback through run_pipeline.py/
# steady_state_pipeline.py. Order matters: a later marker also retroactively
# marks every earlier step "done" (see _run_log), so this only needs each
# step's *first* recognizable log line, not an explicit "finished" one.
DECAY_STEPS = [
    "Generate mesh", "Write initial fields", "Converge flow field",
    "Compute fluence & UV zones", "Run pimpleFoam (decay)", "Post-process & write results",
]
_DECAY_MARKERS = [
    ("Running blockMesh", "Generate mesh"),
    ("Writing initial fields", "Write initial fields"),
    ("Converging flow field", "Converge flow field"),
    ("Computing fluence rate", "Compute fluence & UV zones"),
    ("Running pimpleFoam", "Run pimpleFoam (decay)"),
    ("Running postProcess volAverage", "Post-process & write results"),
]

STEADY_STATE_STEPS = [
    "Set up mesh, flow field, and UV zones", "Carve contaminant source zone",
    "Phase 1: source only (no UV)", "Phase 2: source + UV", "Write results",
]
_STEADY_STATE_MARKERS = [
    ("Setting up mesh, flow field", "Set up mesh, flow field, and UV zones"),
    ("Carving source cellZone", "Carve contaminant source zone"),
    ("Phase 1: source only", "Phase 1: source only (no UV)"),
    ("Phase 2: source + UV", "Phase 2: source + UV"),
]

# "Continue" reuses the existing mesh/flow field/UV zones untouched (see
# _continue_decay) - only pimpleFoam and the post-processing/results steps
# rerun, so it gets its own short checklist rather than DECAY_STEPS' full one.
CONTINUE_STEPS = ["Run pimpleFoam (decay)", "Post-process & write results"]
_CONTINUE_MARKERS = [
    ("Running pimpleFoam", "Run pimpleFoam (decay)"),
    ("Running postProcess volAverage", "Post-process & write results"),
]

_TIME_RE = re.compile(r"^Time\s*=\s*([\d.eE+-]+)\s*$")

# Each pattern's single capture group is the target Time value for the
# phase/chunk that log line announces the start of - matched against the
# exact log_fn messages the pipeline already emits (see converge_flow_field,
# steady_state_pipeline._run_phase, and _run_decay's pimpleFoam line below),
# so an ETA can be computed from (current Time / target) without threading
# a separate progress callback through run_pipeline.py/steady_state_pipeline.py.
_PHASE_TARGET_PATTERNS = [
    re.compile(r"Running simpleFoam \((\d+) iterations, writing every"),  # steady-state phase
    re.compile(r"Running pimpleFoam to ([\d.]+)s"),  # decay transient run
]

# Flow convergence is special-cased rather than folded into
# _PHASE_TARGET_PATTERNS above: each simpleFoam chunk always logs its own
# "Time" starting back at (near) 0 - see converge_flow_field's docstring, the
# chunk's fields carry over but its solver-internal Time counter doesn't - so
# naively treating each chunk as its own phase (old behavior) made the
# progress fraction *shrink* every chunk (500/500, then 500/1000, 500/1500,
# ...) instead of climbing. Fixed by anchoring target_time once to the whole
# budget (_FLOW_BUDGET_RE, logged once at the start of converge_flow_field)
# and tracking a running chunk_base offset (_FLOW_CHUNK_RE, logged at the
# start of every chunk) added to each chunk's local Time to get a true
# cumulative iteration count.
_FLOW_BUDGET_RE = re.compile(r"Flow-convergence budget: (\d+) iterations max")
_FLOW_CHUNK_RE = re.compile(r"Running simpleFoam iterations (\d+)-\d+ \(chunk size")


def _reset_run_progress(sim_type):
    if sim_type == "decay":
        steps, markers = DECAY_STEPS, _DECAY_MARKERS
    elif sim_type == "continue":
        steps, markers = CONTINUE_STEPS, _CONTINUE_MARKERS
    else:
        steps, markers = STEADY_STATE_STEPS, _STEADY_STATE_MARKERS
    _run_state.update(
        sim_type=sim_type, steps=steps, markers=markers,
        step_status={s: "pending" for s in steps}, log=[],
        current_time=None, target_time=None, phase_start_time=None, chunk_base=None,
        start_time=time.time(), stop_requested=False,
    )


def _complete_all_steps():
    for s in _run_state.get("steps", []):
        _run_state["step_status"][s] = "done"


def _should_stop():
    return _run_state.get("stop_requested", False)


_MAX_LOG_LINES = 5000


def _run_log(msg):
    msg = str(msg)
    log = _run_state["log"]
    log.append(msg)
    if len(log) > _MAX_LOG_LINES:
        # Streaming solver output line-by-line (vs. the old tail-20 dump)
        # means a long run can produce tens of thousands of lines - cap
        # memory growth while keeping plenty of scrollback.
        del log[: len(log) - _MAX_LOG_LINES]

    m = _FLOW_BUDGET_RE.search(msg)
    if m:
        _run_state["target_time"] = float(m.group(1))
        _run_state["phase_start_time"] = time.time()
        _run_state["current_time"] = None
        _run_state["chunk_base"] = 0
    else:
        m = _FLOW_CHUNK_RE.search(msg)
        if m:
            _run_state["chunk_base"] = float(m.group(1)) - 1
        else:
            for pattern in _PHASE_TARGET_PATTERNS:
                m = pattern.search(msg)
                if m:
                    _run_state["target_time"] = float(m.group(1))
                    _run_state["phase_start_time"] = time.time()
                    _run_state["current_time"] = None
                    _run_state["chunk_base"] = None
                    break

    for line in msg.splitlines():
        m = _TIME_RE.match(line.strip())
        if m:
            base = _run_state.get("chunk_base")
            _run_state["current_time"] = str(float(m.group(1)) + base) if base is not None else m.group(1)

    steps = _run_state.get("steps", [])
    for substr, step_name in _run_state.get("markers", []):
        if substr in msg and step_name in steps:
            idx = steps.index(step_name)
            for i, s in enumerate(steps):
                _run_state["step_status"][s] = "done" if i < idx else "running" if i == idx else \
                    _run_state["step_status"].get(s, "pending")
            break


def _fan_kwargs(settings):
    if not settings["fan-enable"]:
        return {}
    direction = (0, 0, -1) if settings["fan-direction"] == "down" else (0, 0, 1)
    return dict(
        fan_speed=settings["fan-speed"],
        fan_center=(settings["fan-x-input"], settings["fan-y-input"], settings["fan-z-input"]),
        fan_direction=direction,
        fan_disk_radius=settings["fan-radius"],
        fan_disk_thickness=settings["fan-thickness"],
    )


# Settings that determine the mesh/flow field/UV zones a full Run builds -
# everything Continue reuses as-is without regenerating. If any of these
# differ between what's on disk and what the GUI currently shows, Continue
# would silently apply the OLD values (not what the user now sees in the
# form) since it only touches pimpleFoam. pimple-end-time/write-interval are
# deliberately excluded - changing those is the whole point of Continue.
_MESH_AFFECTING_FIELDS = [
    "ach", "z-value",
    "inlet-wall", "inlet-y-input", "inlet-z-input", "inlet-size-w", "inlet-size-h",
    "outlet-wall", "outlet-y-input", "outlet-z-input", "outlet-size-w", "outlet-size-h",
    "fan-enable", "fan-speed", "fan-direction", "fan-radius", "fan-thickness",
    "fan-x-input", "fan-y-input", "fan-z-input",
]


def _save_run_settings(case_dir, settings):
    with open(f"{case_dir}/run_settings.json", "w") as f:
        json.dump({k: settings.get(k) for k in _MESH_AFFECTING_FIELDS}, f, indent=2)


def _settings_mismatch(case_dir, current_settings):
    """Compare current GUI settings against what the case directory's mesh/
    flow field were actually last built with (see _save_run_settings).
    Returns a list of (field, prior_value, current_value) tuples for
    anything that differs; [] if nothing differs or there's no prior record
    to compare against (an older case dir predating this check, say).
    """
    path = f"{case_dir}/run_settings.json"
    if not Path(path).exists():
        return []
    with open(path) as f:
        prior = json.load(f)
    return [(field, prior[field], current_settings.get(field))
            for field in _MESH_AFFECTING_FIELDS
            if field in prior and prior[field] != current_settings.get(field)]


def _run_decay(guv_path, case_dir, room, settings):
    summary = setup_case(
        guv_path, case_dir, template_case_dir=TEMPLATE_CASE_DIR,
        Z=settings["z-value"], ach=settings["ach"],
        inlet_wall=settings["inlet-wall"],
        inlet_center=(settings["inlet-y-input"] / room.y, settings["inlet-z-input"] / room.z),
        inlet_size=(settings["inlet-size-w"], settings["inlet-size-h"]),
        outlet_wall=settings["outlet-wall"],
        outlet_center=(settings["outlet-y-input"] / room.y, settings["outlet-z-input"] / room.z),
        outlet_size=(settings["outlet-size-w"], settings["outlet-size-h"]),
        pimple_end_time=settings["pimple-end-time"],
        pimple_write_interval=settings["pimple-write-interval"],
        log_fn=_run_log, should_stop=_should_stop,
        **_fan_kwargs(settings),
    )
    if _should_stop():
        raise StoppedByUser("Stopped after case setup.")

    # Record what the mesh/flow field were actually built with, regardless
    # of whether pimpleFoam below succeeds - Continue compares against this,
    # not against whatever the GUI happens to show later.
    _save_run_settings(case_dir, settings)

    case_dir_wsl = wsl_path(case_dir)
    _run_log(f"Running pimpleFoam to {settings['pimple-end-time']}s (this can take a while)...")
    r = run_wsl_streaming(
        "pimpleFoam 2>&1 | tee log.pimpleFoam", case_dir_wsl,
        on_line=_run_log, should_stop=_should_stop, kill_pattern="pimpleFoam",
    )
    if _should_stop():
        raise StoppedByUser("Stopped during pimpleFoam.")
    if r.returncode != 0 or "FOAM FATAL" in r.stdout or "Floating Point Exception" in r.stdout:
        tail = "\n".join(r.stdout.splitlines()[-25:]) or "(no output captured)"
        raise RuntimeError(f"pimpleFoam failed (exit {r.returncode}):\n{tail}")

    _run_log("Running postProcess volAverage...")
    run_wsl_or_raise("postProcess -dict system/volAverageDict", case_dir_wsl, "postProcess volAverage")

    _run_log("Writing results summary...")
    results = write_results_summary(
        case_dir, f"{case_dir}/results.json", settings["ach"],
        summary["eACH_uv_well_mixed_mean"], extra={"n_lamps": summary["n_lamps"]},
    )
    _complete_all_steps()
    _run_log(f"Done. eACH_uv effective={results['eACH_uv_effective']:.4g} /hr "
             f"(well-mixed={results['eACH_uv_well_mixed']:.4g} /hr)")


def _continue_decay(case_dir, end_time, write_interval):
    """Extend an already-completed decay run to a longer duration, reusing
    the existing mesh/converged flow field/UV zones as-is - only pimpleFoam
    (and the postProcess/results steps after it) reruns.

    Two controlDict states are needed, not one: startFrom=latestTime makes
    the *solver* resume from whatever time directory is already on disk
    (verified: it genuinely continues the physics, not just relabeling t=0).
    But postProcess -dict system/volAverageDict honors that same setting for
    its own processing range too - left on latestTime, it only recomputes
    the single newest time step rather than the whole curve (verified
    directly: postProcessing/volAverage1/90/ instead of the expected .../0/,
    containing just one row). So startFrom is switched back to startTime
    (endTime stays at the new, higher value) before postProcess runs, so it
    walks the full 0..end_time history and produces one continuous merged
    decay curve - not something that needs manual stitching in Python.
    """
    results_path = f"{case_dir}/results.json"
    if not Path(results_path).exists():
        raise RuntimeError(
            f"No existing results.json in {case_dir} - run a full simulation "
            f"here first before continuing it."
        )
    with open(results_path) as f:
        prior = json.load(f)

    case_dir_wsl = wsl_path(case_dir)
    _run_log(f"Resuming from the latest existing time directory, extending to {end_time}s "
             f"(mesh, flow field, and UV zones are untouched)...")
    set_control_dict_start_from(case_dir, "latestTime")
    set_control_dict_time(case_dir, end_time=end_time, write_interval=write_interval)

    _run_log(f"Running pimpleFoam to {end_time}s...")
    r = run_wsl_streaming(
        "pimpleFoam 2>&1 | tee -a log.pimpleFoam", case_dir_wsl,
        on_line=_run_log, should_stop=_should_stop, kill_pattern="pimpleFoam",
    )
    if _should_stop():
        raise StoppedByUser("Stopped during pimpleFoam.")
    if r.returncode != 0 or "FOAM FATAL" in r.stdout or "Floating Point Exception" in r.stdout:
        tail = "\n".join(r.stdout.splitlines()[-25:]) or "(no output captured)"
        raise RuntimeError(f"pimpleFoam failed (exit {r.returncode}):\n{tail}")

    _run_log("Running postProcess volAverage (recomputing the full merged decay curve)...")
    set_control_dict_start_from(case_dir, "startTime")
    run_wsl_or_raise("rm -rf postProcessing", case_dir_wsl, "clearing stale postProcessing")
    run_wsl_or_raise("postProcess -dict system/volAverageDict", case_dir_wsl, "postProcess volAverage")

    _run_log("Writing results summary...")
    extra = {k: prior[k] for k in ("n_lamps",) if k in prior}
    results = write_results_summary(
        case_dir, results_path, prior["ventilation_ach"], prior["eACH_uv_well_mixed"],
        extra=extra or None,
    )
    _complete_all_steps()
    _run_log(f"Done. eACH_uv effective={results['eACH_uv_effective']:.4g} /hr "
             f"(well-mixed={results['eACH_uv_well_mixed']:.4g} /hr)")


def _settling_iterations(lambda_per_hr, target_fraction=0.995, min_iterations=500, max_iterations=50000):
    """Iterations to settle to target_fraction of steady state for a
    first-order well-mixed system (dT/dt = G/V - lambda*T): t = ln(1/(1-f))/lambda.
    _run_phase() uses deltaT=1s per iteration, so this iteration count IS
    the settling time in seconds directly - no separate unit conversion.
    lambda_per_hr is the total removal rate (ventilation ACH, plus UV's
    eACH for phase 2) in 1/hr.
    """
    if lambda_per_hr <= 0:
        return max_iterations
    lambda_per_s = lambda_per_hr / 3600.0
    t = math.log(1.0 / (1.0 - target_fraction)) / lambda_per_s
    return int(min(max_iterations, max(min_iterations, round(t))))


def _run_steady_state(guv_path, case_dir, room, settings):
    fan_kwargs = _fan_kwargs(settings)

    _run_log("=== Setting up mesh, flow field, and UV zones ===")
    summary = setup_case(
        guv_path, case_dir, template_case_dir=TEMPLATE_CASE_DIR,
        Z=settings["z-value"], ach=settings["ach"],
        inlet_wall=settings["inlet-wall"],
        inlet_center=(settings["inlet-y-input"] / room.y, settings["inlet-z-input"] / room.z),
        inlet_size=(settings["inlet-size-w"], settings["inlet-size-h"]),
        outlet_wall=settings["outlet-wall"],
        outlet_center=(settings["outlet-y-input"] / room.y, settings["outlet-z-input"] / room.z),
        outlet_size=(settings["outlet-size-w"], settings["outlet-size-h"]),
        log_fn=_run_log, should_stop=_should_stop,
        **fan_kwargs,
    )
    if _should_stop():
        raise StoppedByUser("Stopped after case setup.")

    fan_entry = None
    if settings["fan-enable"]:
        fan_entry = fan_fvoptions_entry(settings["fan-speed"], direction=fan_kwargs["fan_direction"])

    inlet_area = settings["inlet-size-w"] * settings["inlet-size-h"]
    room_volume = room.x * room.y * room.z
    inflow_dir = (1, 0, 0) if settings["inlet-wall"] == "xMin" else (-1, 0, 0)
    v_mag = compute_inlet_velocity(settings["ach"], room_volume, inlet_area)
    inlet_velocity = tuple(v_mag * d for d in inflow_dir)

    ach = settings["ach"]
    eACH_uv = summary.get("eACH_uv_well_mixed_mean", 0.0)
    phase1_iterations = max(settings["phase1-iterations"], _settling_iterations(ach))
    phase2_iterations = max(settings["phase2-iterations"], _settling_iterations(ach + eACH_uv))
    _run_log(f"99.5% settling estimate: phase1={_settling_iterations(ach)} iterations "
             f"(ACH={ach:.3g}/hr alone), phase2={_settling_iterations(ach + eACH_uv)} iterations "
             f"(ACH+eACH_uv={ach + eACH_uv:.3g}/hr) - using the larger of this and the configured "
             f"value for each phase ({phase1_iterations}, {phase2_iterations}).")

    result = run_steady_state_scenario(
        case_dir, room.x, room.y, room.z, settings["ach"], settings["z-value"],
        source_center=(settings["inject-x-input"], settings["inject-y-input"], settings["inject-z-input"]),
        target_T_ss=settings["target-t-ss"],
        inlet_velocity=inlet_velocity,
        phase1_iterations=phase1_iterations,
        phase2_iterations=phase2_iterations,
        fan_entry=fan_entry,
        log_fn=_run_log, should_stop=_should_stop,
    )
    with open(f"{case_dir}/results.json", "w") as f:
        json.dump(result, f, indent=2)
    _complete_all_steps()
    _run_log(f"Done. Reduction={result['reduction_pct']:.1f}%, "
             f"eACH_uv={result['eACH_uv_steady_state']:.4g} /hr")


def _run_pipeline_thread(sim_type, guv_path, case_dir, room, settings):
    try:
        if sim_type == "decay":
            _run_decay(guv_path, case_dir, room, settings)
        else:
            _run_steady_state(guv_path, case_dir, room, settings)
        _run_state["status"] = "done"
    except StoppedByUser as e:
        _run_log(f"Stopped: {e}")
        _run_state["status"] = "stopped"
    except Exception as e:
        _run_log(f"ERROR: {e}")
        _run_state["status"] = "error"


def _continue_pipeline_thread(case_dir, end_time, write_interval):
    try:
        _continue_decay(case_dir, end_time, write_interval)
        _run_state["status"] = "done"
    except StoppedByUser as e:
        _run_log(f"Stopped: {e}")
        _run_state["status"] = "stopped"
    except Exception as e:
        _run_log(f"ERROR: {e}")
        _run_state["status"] = "error"


app = dash.Dash(__name__, external_stylesheets=[dbc.themes.FLATLY])
app.title = "GUV-CFD"


def _native_open_file(filetypes, title, initialdir=None):
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    kwargs = {"title": title, "filetypes": filetypes}
    if initialdir:
        kwargs["initialdir"] = initialdir
    path = filedialog.askopenfilename(**kwargs)
    root.destroy()
    # Tk returns forward-slash paths on Windows even for UNC (\\wsl.localhost\...)
    # paths - normalize so downstream code doesn't have to handle both forms.
    return path.replace("/", "\\") if path else None


def _native_choose_dir(title, initialdir=None):
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    kwargs = {"title": title}
    if initialdir:
        kwargs["initialdir"] = initialdir
    path = filedialog.askdirectory(**kwargs)
    root.destroy()
    return path.replace("/", "\\") if path else None


def _native_save_file(title, defaultextension, filetypes):
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.asksaveasfilename(
        title=title, defaultextension=defaultextension, filetypes=filetypes,
    )
    root.destroy()
    return path or None


def _empty_preview_figure():
    return go.Figure(layout=dict(
        annotations=[dict(text="Load a .guv project to preview the case",
                           showarrow=False, font=dict(size=16, color="#888"))],
    ))


def _card(title, children):
    return dbc.Card(
        [dbc.CardHeader(title, className="fw-semibold small text-uppercase"),
         dbc.CardBody(children)],
        className="mb-3",
    )


def _labeled(label, component, help_text=None):
    children = [html.Label(label, className="form-label small mb-1"), component]
    if help_text:
        children.append(html.Div(help_text, className="form-text small"))
    return html.Div(children, className="mb-2")


def _position_field(prefix, label, default, minv, maxv, step):
    return _labeled(label, dbc.Row([
        dbc.Col(dcc.Slider(id=f"{prefix}-slider", min=minv, max=maxv, step=step, value=default,
                            marks=None, tooltip={"placement": "bottom", "always_visible": False}),
                width=8, className="pt-1"),
        dbc.Col(dcc.Input(id=f"{prefix}-input", type="number", value=default, min=minv, max=maxv,
                           step=step, className="form-control form-control-sm"), width=4),
    ], align="center", className="g-2"))


def _position_field_component(prefix):
    _, label, _dim, _default_fn, default, minv, maxv, step = _POSITION_FIELD_BY_PREFIX[prefix]
    return _position_field(prefix, label, default, minv, maxv, step)


def _opening_controls(prefix, default_wall):
    return [
        dbc.Checkbox(id=f"{prefix}-show", value=True, label="Show in preview", className="mb-2"),
        _labeled("Wall", dcc.Dropdown(id=f"{prefix}-wall", options=WALL_OPTIONS,
                                       value=default_wall, clearable=False)),
        _position_field_component(f"{prefix}-y"),
        _position_field_component(f"{prefix}-z"),
        _labeled("Opening size, W x H (m)", dbc.Row([
            dbc.Col(dcc.Input(id=f"{prefix}-size-w", type="number", value=0.3,
                               min=0.05, max=2.0, step=0.05, className="form-control form-control-sm")),
            dbc.Col(dcc.Input(id=f"{prefix}-size-h", type="number", value=0.3,
                               min=0.05, max=2.0, step=0.05, className="form-control form-control-sm")),
        ])),
    ]


def _fan_position_controls():
    return [_position_field_component(p) for p in ("fan-x", "fan-y", "fan-z")]


def _injection_position_controls():
    return [_position_field_component(p) for p in ("inject-x", "inject-y", "inject-z")]


project_setup_tab = dbc.Row([
    # --- left column: inputs ---
    dbc.Col([
        _card("Project", [
            dbc.Button("Load .guv file...", id="load-btn", color="primary",
                       size="sm", className="w-100"),
            html.Div(id="project-status", className="small text-muted mt-2"),
            _labeled("Description", dcc.Textarea(
                id="project-description", value="",
                style={"width": "100%", "height": "60px"},
                className="form-control form-control-sm")),
        ]),

        _card("OpenFOAM project directory", [
            _labeled("Project directory (WSL path)", dbc.Row([
                dbc.Col(dcc.Input(
                    id="case-dir", type="text", debounce=True, value=_DEFAULT_RUN_DIR,
                    placeholder=r"\\wsl.localhost\Ubuntu\home\...\run",
                    className="form-control form-control-sm"), width=8),
                dbc.Col(dbc.Button("Browse...", id="browse-case-dir-btn", size="sm",
                                   color="secondary", className="w-100"), width=4),
            ], className="g-2")),
        ]),

        _card("Ventilation & UV", [
            _labeled("Air changes per hour (ACH)", dcc.Input(
                id="ach", type="number", value=3.0, min=0.1, max=20, step=0.1,
                className="form-control form-control-sm")),
            _labeled("Z — UV susceptibility (cm²/mJ)", dcc.Input(
                id="z-value", type="number", value=2.0, min=0.01, max=20, step=0.1,
                className="form-control form-control-sm")),
        ]),

        _card("Inlet", _opening_controls("inlet", "xMin")),

        _card("Outlet", _opening_controls("outlet", "xMax")),

        _card("Mixing fan", [
            dbc.Checkbox(id="fan-enable", value=False, label="Enable fan", className="mb-2"),
            html.Div(id="fan-controls", children=[
                _labeled("Speed (m/s), 0.05–0.5 typical", dcc.Slider(
                    id="fan-speed", min=0.05, max=0.5, step=0.01, value=0.3,
                    marks={0.05: "0.05", 0.5: "0.5"},
                    tooltip={"placement": "bottom", "always_visible": True})),
                _labeled("Direction", dbc.RadioItems(
                    id="fan-direction",
                    className="btn-group w-100",
                    inputClassName="btn-check",
                    labelClassName="btn btn-outline-secondary btn-sm",
                    labelCheckedClassName="active",
                    options=[
                        {"label": "Downward", "value": "down"},
                        {"label": "Upward", "value": "up"},
                    ],
                    value="down",
                )),
                _labeled("Radius (m)", dcc.Input(
                    id="fan-radius", type="number", value=0.6, min=0.1, max=1.5, step=0.05,
                    className="form-control form-control-sm")),
                _labeled("Thickness (m)", dcc.Input(
                    id="fan-thickness", type="number", value=0.2, min=0.05, max=1.0, step=0.05,
                    className="form-control form-control-sm")),
                *_fan_position_controls(),
            ]),
        ]),

        _card("Simulation type", [
            dbc.RadioItems(
                id="sim-type",
                className="btn-group w-100 mb-2",
                inputClassName="btn-check",
                labelClassName="btn btn-outline-primary",
                labelCheckedClassName="active",
                options=[
                    {"label": "Decay", "value": "decay"},
                    {"label": "Steady state", "value": "steady_state"},
                ],
                value="decay",
            ),
            html.Div(id="decay-controls", children=[
                _labeled("Simulated duration (s)", dcc.Input(
                    id="pimple-end-time", type="number", value=120, min=10, max=7200, step=10,
                    className="form-control form-control-sm")),
                _labeled("Write interval (s)", dcc.Input(
                    id="pimple-write-interval", type="number", value=10, min=1, max=600, step=1,
                    className="form-control form-control-sm")),
            ]),
            html.Div(id="steady-state-controls", children=[
                _labeled("Target well-mixed steady-state T", dcc.Input(
                    id="target-t-ss", type="number", value=0.3, min=0.01, max=1.0, step=0.01,
                    className="form-control form-control-sm"),
                    help_text="Injection flow (source strength) is calculated automatically "
                              "from this target and the ACH above."),
                html.Div("Injection position", className="small fw-semibold text-uppercase mt-3 mb-1"),
                *_injection_position_controls(),
                _labeled("Phase 1 iterations (no UV)", dcc.Input(
                    id="phase1-iterations", type="number", value=8000, min=500, max=50000, step=500,
                    className="form-control form-control-sm")),
                _labeled("Phase 2 iterations (UV on)", dcc.Input(
                    id="phase2-iterations", type="number", value=3000, min=500, max=50000, step=500,
                    className="form-control form-control-sm")),
            ]),
        ]),

        dbc.Button("Run simulation", id="run-btn", color="success", className="w-100 mb-2"),
        dbc.Button("Continue to longer duration", id="continue-btn", color="secondary",
                   outline=True, className="w-100 mb-2"),
        html.Div(id="run-validation-msg", className="small text-danger text-center mb-4"),
    ], width=4, style={"maxHeight": "88vh", "overflowY": "auto"}),

    # --- right column: 3D preview ---
    dbc.Col([
        dcc.Graph(id="preview-graph", style={"height": "88vh"}, figure=_empty_preview_figure()),
    ], width=8),
])


def _checklist_item(step):
    return html.Li("☐ " + step, className="text-muted")


processing_tab = dbc.Row([
    dbc.Col([
        html.Div(id="run-status-text", className="fs-5 fw-semibold mb-2"),
        dbc.Progress(id="run-progress-bar", value=0, striped=True, animated=True, className="mb-2"),
        html.Div(id="run-elapsed", className="small text-muted"),
        html.Div(id="run-current-time", className="small text-muted mb-3"),
        dbc.Button("Stop", id="stop-btn", color="danger", size="sm", className="mb-4", disabled=True),
        html.Div("Steps", className="small fw-semibold text-uppercase mb-1"),
        html.Ul([_checklist_item(s) for s in DECAY_STEPS], id="run-checklist", className="list-unstyled small"),
    ], width=4),
    dbc.Col([
        html.Div("Log", className="small fw-semibold text-uppercase mb-1"),
        html.Pre(id="run-log", className="small", style={
            "height": "72vh", "overflowY": "auto", "fontSize": "11px",
            "background": "rgba(127,127,127,0.08)", "padding": "8px",
            "border": "1px solid rgba(127,127,127,0.3)", "whiteSpace": "pre-wrap",
        }),
    ], width=8),
], className="mt-3")

def _empty_analysis_figure():
    return go.Figure(layout=dict(
        annotations=[dict(text="Load a results.json to see analysis (or finish a run - it loads automatically)",
                           showarrow=False, font=dict(size=16, color="#888"))],
    ))


def _steady_state_figure(result):
    """T over time as a percentage of phase 1's steady state (100%), phase
    1 and phase 2 plotted on one continuous linear timeline (phase 2
    shifted to start where phase 1 ends) so the UV-on transition and its
    reduction read directly off the curve. Time axis is linear - the
    underlying OpenFOAM write schedule is what's log-spaced (see
    _settling_write_schedule()), not this plot.
    """
    p1, p2 = result["phase1"], result["phase2"]
    T_ss1 = p1["T_ss"] or 1.0
    t1 = p1["decay_curve"]["t"]
    T1 = p1["decay_curve"]["T"]
    t1_end = t1[-1] if t1 else 0.0

    t2 = p2["decay_curve"]["t"]
    T2 = p2["decay_curve"]["T"]
    t2_shifted = [t1_end + v for v in t2]

    pct1 = [100 * v / T_ss1 for v in T1]
    pct2 = [100 * v / T_ss1 for v in T2]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=t1, y=pct1, mode="lines+markers", name="Phase 1 (no UV)",
                              line=dict(color="#e67e22", width=2)))
    fig.add_trace(go.Scatter(x=t2_shifted, y=pct2, mode="lines+markers", name="Phase 2 (UV on)",
                              line=dict(color="#2ecc71", width=2)))
    fig.add_hline(y=100, line_dash="dot", line_color="gray",
                  annotation_text="Phase 1 steady state (100%)", annotation_position="top left")
    pct2_ss = 100 * p2["T_ss"] / T_ss1
    fig.add_hline(y=pct2_ss, line_dash="dot", line_color="#2ecc71",
                  annotation_text=f"Phase 2 steady state ({pct2_ss:.1f}%)", annotation_position="bottom left")
    fig.add_vline(x=t1_end, line_dash="dash", line_color="gray", annotation_text="UV on")
    fig.update_layout(
        xaxis_title="Time (s)", yaxis_title="T (% of phase 1 steady state)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(l=50, r=20, t=30, b=45),
    )
    return fig


def _steady_state_summary(result):
    p1, p2 = result["phase1"], result["phase2"]
    rows = [
        ("Target T_ss (design)", f"{result.get('target_T_ss', '?')}"),
        ("Phase 1 T_ss", f"{p1['T_ss']:.4g}  ({'plateaued' if p1['converged'] else 'NOT fully plateaued'}, "
                          f"{p1['iterations']} iterations)"),
        ("Phase 2 T_ss", f"{p2['T_ss']:.4g}  ({'plateaued' if p2['converged'] else 'NOT fully plateaued'}, "
                          f"{p2['iterations']} iterations)"),
        ("Reduction", f"{result['reduction_pct']:.1f}%"),
        ("eACH_uv (steady-state method)", f"{result['eACH_uv_steady_state']:.4g} /hr"),
    ]
    return [html.Div([html.Span(k + ": ", className="text-muted"), html.Span(v)], className="mb-1")
            for k, v in rows]


def _decay_figure(result):
    """Actual CFD decay curve plus two idealized well-mixed reference curves
    (pure ventilation, and ventilation+UV at the well-mixed eACH estimate)
    computed from the same T[0] starting value - so the gap between the real
    (CFD) curve and each reference visually shows how much imperfect mixing
    slows disinfection versus the idealized box-model assumption. Log y-axis
    since decay is exponential - a straight line here is a pure exponential,
    and curvature/kinks reveal where the real mixing deviates from one.
    """
    curve = result["decay_curve"]
    t, T = curve["t_seconds"], curve["volAverage_T"]
    T0 = T[0] if T else 1.0

    lambda_vent = result["ventilation_ach"] / 3600.0
    lambda_well_mixed = lambda_vent + result["eACH_uv_well_mixed"] / 3600.0
    ach_curve = [T0 * math.exp(-lambda_vent * ti) for ti in t]
    well_mixed_curve = [T0 * math.exp(-lambda_well_mixed * ti) for ti in t]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=t, y=T, mode="lines+markers", name="volAverage(T) - actual (CFD)",
                              line=dict(color="#3498db", width=2)))
    fig.add_trace(go.Scatter(x=t, y=ach_curve, mode="lines",
                              name=f"Ventilation ACH only ({result['ventilation_ach']:.3g}/hr)",
                              line=dict(color="#95a5a6", width=2, dash="dash")))
    fig.add_trace(go.Scatter(x=t, y=well_mixed_curve, mode="lines",
                              name=f"Well-mixed, ACH+eACH_uv "
                                   f"({result['ventilation_ach'] + result['eACH_uv_well_mixed']:.3g}/hr)",
                              line=dict(color="#e67e22", width=2, dash="dash")))
    fig.update_layout(
        xaxis_title="Time (s)", yaxis_title="volAverage(T)", yaxis_type="log",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(l=50, r=20, t=30, b=45),
    )
    return fig


def _decay_summary(result):
    rows = [
        ("Ventilation ACH", f"{result['ventilation_ach']:.3g} /hr"),
        ("eACH_uv well-mixed", f"{result['eACH_uv_well_mixed']:.4g} /hr"),
        ("eACH_uv effective (CFD-fit)", f"{result['eACH_uv_effective']:.4g} /hr"),
        ("Total ACH, effective", f"{result.get('total_ach_effective', 0):.3g} /hr"),
    ]
    if result.get("mixing_efficiency") is not None:
        rows.append(("Mixing efficiency", f"{result['mixing_efficiency'] * 100:.1f}%"))
    return [html.Div([html.Span(k + ": ", className="text-muted"), html.Span(v)], className="mb-1")
            for k, v in rows]


analysis_tab = dbc.Row([
    dbc.Col([
        dbc.Button("Load results.json...", id="load-results-btn", color="primary",
                   size="sm", className="w-100 mb-2"),
        html.Div(id="analysis-status", className="small text-muted mb-3"),
        html.Div(id="analysis-summary", className="small"),
    ], width=4),
    dbc.Col([
        dcc.Graph(id="analysis-graph", style={"height": "80vh"}, figure=_empty_analysis_figure()),
    ], width=8),
], className="mt-3")

app.layout = dbc.Container([
    dcc.Store(id="fresh-room-load"),
    dcc.Store(id="results-data"),
    dcc.Interval(id="run-poll", interval=2000, n_intervals=0, disabled=True),
    dcc.ConfirmDialog(id="overwrite-confirm"),
    dbc.Row([
        dbc.Col(html.H4("GUV-CFD", className="mt-3 mb-1"), width="auto"),
        dbc.Col(dbc.DropdownMenu(
            label="File", color="light", size="sm", className="mt-3",
            children=[
                dbc.DropdownMenuItem("Open Project...", id="menu-open"),
                dbc.DropdownMenuItem("Save Project", id="menu-save"),
                dbc.DropdownMenuItem("Save Project As...", id="menu-save-as"),
            ],
        ), width="auto"),
        dbc.Col(html.Div("Untitled project", id="project-name-display",
                          className="mt-3 text-muted fst-italic"), width="auto"),
    ], align="center", className="g-3"),
    dbc.Row(dbc.Col(html.Div(
        "guv-calcs UV fluence × OpenFOAM CFD — configure a case, preview it, then run.",
        className="text-muted small mb-3",
    ))),
    dbc.Tabs([
        dbc.Tab(project_setup_tab, label="Project Setup", tab_id="project-setup"),
        dbc.Tab(processing_tab, label="Processing", tab_id="processing"),
        dbc.Tab(analysis_tab, label="Analysis of Results", tab_id="analysis"),
    ], active_tab="project-setup", className="mb-3", id="main-tabs"),
], fluid=True)


# --- two-way slider<->number sync + reset-to-room-dimensions on load,
# one callback per position field (registered in a loop). ---
def _register_position_field(prefix, dim, default_fn):
    @app.callback(
        Output(f"{prefix}-slider", "value"),
        Output(f"{prefix}-input", "value"),
        Output(f"{prefix}-slider", "max"),
        Output(f"{prefix}-input", "max"),
        Input(f"{prefix}-slider", "value"),
        Input(f"{prefix}-input", "value"),
        Input("fresh-room-load", "data"),
        prevent_initial_call=True,
    )
    def _sync(slider_val, input_val, _fresh_load):
        # Only "Load .guv file..." (a genuinely new room, no saved positions
        # to restore) fires fresh-room-load - "Open Project" restores exact
        # saved values itself and updates max directly, bypassing this reset.
        trig = dash.ctx.triggered_id
        if trig == "fresh-room-load":
            room = _loaded["room"]
            if room is None:
                return dash.no_update, dash.no_update, dash.no_update, dash.no_update
            dim_size = round(getattr(room, dim), 3)
            default = round(default_fn(room), 3)
            return default, default, dim_size, dim_size
        if trig == f"{prefix}-slider":
            return dash.no_update, slider_val, dash.no_update, dash.no_update
        return input_val, dash.no_update, dash.no_update, dash.no_update

    _sync.__name__ = f"_sync_{prefix.replace('-', '_')}"


for _prefix, _label, _dim, _default_fn, *_rest in POSITION_FIELDS:
    _register_position_field(_prefix, _dim, _default_fn)


@app.callback(
    Output("decay-controls", "style"),
    Output("steady-state-controls", "style"),
    Input("sim-type", "value"),
)
def _toggle_sim_type_controls(sim_type):
    if sim_type == "decay":
        return {"display": "block"}, {"display": "none"}
    return {"display": "none"}, {"display": "block"}


@app.callback(
    Output("fan-controls", "style"),
    Input("fan-enable", "value"),
)
def _toggle_fan_controls(enabled):
    return {"display": "block"} if enabled else {"display": "none", "opacity": "0.4"}


@app.callback(
    Output("project-status", "children"),
    Output("project-description", "value"),
    Output("fresh-room-load", "data"),
    Output("case-dir", "value", allow_duplicate=True),
    Input("load-btn", "n_clicks"),
    prevent_initial_call=True,
)
def _load_project(n_clicks):
    path = _native_open_file(
        [("GUV project files", "*.guv"), ("All files", "*.*")],
        "Select a .guv project file",
    )
    if not path:
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update
    try:
        project = Project.load(path)
        room = next(iter(project.rooms.values()))
    except Exception as e:
        return f"Failed to load: {e}", dash.no_update, dash.no_update, dash.no_update
    _loaded["project"] = project
    _loaded["room"] = room
    _loaded["path"] = path
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    status = f"Loaded {name}: {room.x:.2f} x {room.y:.2f} x {room.z:.2f} {room.units}, {len(room.lamps)} lamp(s)"
    description = f"{room.x:.2f} x {room.y:.2f} x {room.z:.2f} {room.units} room"
    return status, description, n_clicks, _fresh_case_dir(path)


@app.callback(
    Output("case-dir", "value"),
    Input("browse-case-dir-btn", "n_clicks"),
    State("case-dir", "value"),
    prevent_initial_call=True,
)
def _browse_case_dir(n_clicks, current_dir):
    path = _native_choose_dir("Select or create an OpenFOAM project directory",
                               initialdir=current_dir)
    if not path:
        return dash.no_update
    return path


@app.callback(
    Output("results-data", "data", allow_duplicate=True),
    Output("analysis-status", "children"),
    Input("load-results-btn", "n_clicks"),
    State("case-dir", "value"),
    prevent_initial_call=True,
)
def _load_results(n_clicks, case_dir_field):
    # Prefer the directory of the run that actually just happened (this
    # session), falling back to whatever's in the project-directory field -
    # either way, start in the WSL-mapped project folder, not Tk's default.
    initialdir = _run_state.get("case_dir") or case_dir_field or None
    path = _native_open_file(
        [("Results JSON", "*.json"), ("All files", "*.*")],
        "Select a results.json file",
        initialdir=initialdir,
    )
    if not path:
        return dash.no_update, dash.no_update
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:
        return dash.no_update, f"Failed to load: {e}"
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return data, f"Loaded {name}"


@app.callback(
    Output("analysis-graph", "figure"),
    Output("analysis-summary", "children"),
    Input("results-data", "data"),
)
def _render_analysis(data):
    if not data:
        return _empty_analysis_figure(), []
    if "phase1" in data:
        return _steady_state_figure(data), _steady_state_summary(data)
    return _decay_figure(data), _decay_summary(data)


@app.callback(
    Output("project-name-display", "children"),
    Input("menu-save", "n_clicks"),
    Input("menu-save-as", "n_clicks"),
    [State(fid, "value") for fid in SETTINGS_FIELDS],
    prevent_initial_call=True,
)
def _save_project(n_save, n_save_as, *values):
    trig = dash.ctx.triggered_id
    settings = dict(zip(SETTINGS_FIELDS, values))
    settings["guv_path"] = _loaded.get("path")

    path = _loaded.get("settings_path")
    if trig == "menu-save-as" or not path:
        path = _native_save_file(
            "Save GUV-CFD project",
            ".guvcfd",
            [("GUV-CFD project files", "*.guvcfd"), ("All files", "*.*")],
        )
        if not path:
            return dash.no_update

    with open(path, "w") as f:
        json.dump(settings, f, indent=2)
    _loaded["settings_path"] = path
    return path.replace("\\", "/").rsplit("/", 1)[-1]


_open_outputs = [
    Output("project-name-display", "children", allow_duplicate=True),
    Output("project-status", "children", allow_duplicate=True),
]
_open_outputs += [Output(fid, "value", allow_duplicate=True) for fid in SETTINGS_FIELDS]
for _prefix, *_ in POSITION_FIELDS:
    _open_outputs.append(Output(f"{_prefix}-slider", "max", allow_duplicate=True))
    _open_outputs.append(Output(f"{_prefix}-input", "max", allow_duplicate=True))


@app.callback(
    *_open_outputs,
    Input("menu-open", "n_clicks"),
    prevent_initial_call=True,
)
def _open_project(n_clicks):
    n_outputs = len(_open_outputs)
    no_change = tuple(dash.no_update for _ in range(n_outputs))

    path = _native_open_file(
        [("GUV-CFD project files", "*.guvcfd"), ("All files", "*.*")],
        "Open a GUV-CFD project",
    )
    if not path:
        return no_change

    try:
        with open(path) as f:
            settings = json.load(f)
    except Exception as e:
        result = list(no_change)
        result[1] = f"Failed to open project: {e}"
        return tuple(result)

    guv_path = settings.get("guv_path")
    status = "No .guv file recorded in this project."
    room = None
    if guv_path:
        try:
            project = Project.load(guv_path)
            room = next(iter(project.rooms.values()))
            _loaded["project"] = project
            _loaded["room"] = room
            _loaded["path"] = guv_path
            gname = guv_path.replace("\\", "/").rsplit("/", 1)[-1]
            status = (f"Loaded {gname}: {room.x:.2f} x {room.y:.2f} x {room.z:.2f} "
                      f"{room.units}, {len(room.lamps)} lamp(s)")
        except Exception as e:
            status = f"Failed to reload {guv_path}: {e}"

    _loaded["settings_path"] = path
    proj_name = path.replace("\\", "/").rsplit("/", 1)[-1]

    field_values = [settings.get(fid) for fid in SETTINGS_FIELDS]
    max_values = []
    for _prefix, _label, dim, _default_fn, *_rest in POSITION_FIELDS:
        if room is not None:
            dim_size = round(getattr(room, dim), 3)
            max_values += [dim_size, dim_size]
        else:
            max_values += [dash.no_update, dash.no_update]

    return tuple([proj_name, status] + field_values + max_values)


def _case_dir_has_data(case_dir):
    """True if case_dir already looks like it holds a completed or
    in-progress run (a results.json, or any real solver time directory
    beyond 0/) - used to warn before a fresh Run regenerates the mesh and
    silently overwrites/orphans it. Not a guarantee of what a fresh run
    would actually delete (see _continue_decay's docstring - simpleFoam's
    own chunk cleanup deletes non-0/ time directories, but only if it gets
    far enough to run), just a "there's something here" heuristic.
    """
    p = Path(case_dir)
    if (p / "results.json").exists():
        return True
    if not p.exists():
        return False
    return any(c.is_dir() and c.name != "0" and re.fullmatch(r"\d+(\.\d+)?", c.name)
               for c in p.iterdir())


# Holds a Run click's settings between the overwrite-confirmation prompt and
# the user's confirm click (two separate callbacks/requests) - single-user
# local tool, so plain module state is fine here (same pattern as _run_state).
_pending_run = {"sim_type": None, "guv_path": None, "case_dir": None, "room": None, "settings": None}


def _launch_run(sim_type, guv_path, case_dir, room, settings):
    _reset_run_progress(sim_type)
    _run_state["status"] = "running"
    _run_state["case_dir"] = case_dir
    thread = threading.Thread(
        target=_run_pipeline_thread,
        args=(sim_type, guv_path, case_dir, room, settings),
        daemon=True,
    )
    thread.start()


@app.callback(
    Output("run-btn", "disabled"),
    Output("continue-btn", "disabled", allow_duplicate=True),
    Output("run-poll", "disabled", allow_duplicate=True),
    Output("run-validation-msg", "children"),
    Output("main-tabs", "active_tab"),
    Output("overwrite-confirm", "displayed"),
    Output("overwrite-confirm", "message"),
    Input("run-btn", "n_clicks"),
    [State(fid, "value") for fid in SETTINGS_FIELDS],
    prevent_initial_call=True,
)
def _start_run(n_clicks, *values):
    if _run_state["status"] == "running":
        return True, True, False, dash.no_update, "processing", False, dash.no_update

    room = _loaded["room"]
    guv_path = _loaded["path"]
    if room is None or guv_path is None:
        return (False, False, True, "No .guv project loaded - use File > Open Project or "
                "Load .guv file first.", dash.no_update, False, dash.no_update)

    settings = dict(zip(SETTINGS_FIELDS, values))
    case_dir = settings["case-dir"]
    if not case_dir:
        return (False, False, True, "Set an OpenFOAM project directory first.",
                dash.no_update, False, dash.no_update)

    sim_type = settings["sim-type"]

    if _case_dir_has_data(case_dir):
        _pending_run.update(sim_type=sim_type, guv_path=guv_path, case_dir=case_dir,
                             room=room, settings=settings)
        return (False, False, True, "", dash.no_update, True,
                f"{case_dir} already has simulation data (results.json and/or solver "
                f"output). Running will regenerate the mesh and overwrite the case "
                f"directory in place - existing results may be lost. Continue anyway?")

    _launch_run(sim_type, guv_path, case_dir, room, settings)
    return True, True, False, "", "processing", False, dash.no_update


@app.callback(
    Output("run-btn", "disabled", allow_duplicate=True),
    Output("continue-btn", "disabled", allow_duplicate=True),
    Output("run-poll", "disabled", allow_duplicate=True),
    Output("main-tabs", "active_tab", allow_duplicate=True),
    Input("overwrite-confirm", "submit_n_clicks"),
    prevent_initial_call=True,
)
def _confirm_overwrite_run(submit_n_clicks):
    if not _pending_run.get("case_dir"):
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update
    _launch_run(_pending_run["sim_type"], _pending_run["guv_path"], _pending_run["case_dir"],
                _pending_run["room"], _pending_run["settings"])
    _pending_run.update(sim_type=None, guv_path=None, case_dir=None, room=None, settings=None)
    return True, True, False, "processing"


@app.callback(
    Output("continue-btn", "disabled"),
    Output("run-btn", "disabled", allow_duplicate=True),
    Output("run-poll", "disabled", allow_duplicate=True),
    Output("run-validation-msg", "children", allow_duplicate=True),
    Output("main-tabs", "active_tab", allow_duplicate=True),
    Input("continue-btn", "n_clicks"),
    State("case-dir", "value"),
    State("sim-type", "value"),
    State("pimple-end-time", "value"),
    State("pimple-write-interval", "value"),
    [State(fid, "value") for fid in _MESH_AFFECTING_FIELDS],
    prevent_initial_call=True,
)
def _start_continue(n_clicks, case_dir, sim_type, end_time, write_interval, *mesh_values):
    if _run_state["status"] == "running":
        return True, True, False, dash.no_update, "processing"

    if sim_type != "decay":
        return (False, False, True, "Continuing to a longer duration is only supported "
                "for Decay Curve runs.", dash.no_update)
    if not case_dir:
        return False, False, True, "Set an OpenFOAM project directory first.", dash.no_update
    if not Path(f"{case_dir}/results.json").exists():
        return (False, False, True, "No completed run found in this directory yet - run a "
                "full simulation first, then use Continue to extend it.", dash.no_update)

    mismatches = _settings_mismatch(case_dir, dict(zip(_MESH_AFFECTING_FIELDS, mesh_values)))
    if mismatches:
        changed = "; ".join(f"{field} was {prior}, now {current}"
                             for field, prior, current in mismatches)
        return (False, False, True,
                f"These settings differ from the run currently on disk, and Continue won't "
                f"apply them (it only reruns pimpleFoam - mesh/flow field/UV zones are reused "
                f"as-is): {changed}. Run a full simulation instead if you want these changes "
                f"to take effect.", dash.no_update)

    _reset_run_progress("continue")
    _run_state["status"] = "running"
    _run_state["case_dir"] = case_dir

    thread = threading.Thread(
        target=_continue_pipeline_thread,
        args=(case_dir, end_time, write_interval),
        daemon=True,
    )
    thread.start()
    return True, True, False, "", "processing"


@app.callback(
    Output("run-log", "children", allow_duplicate=True),
    Input("stop-btn", "n_clicks"),
    prevent_initial_call=True,
)
def _stop_run(n_clicks):
    if _run_state["status"] == "running":
        _run_state["stop_requested"] = True
        _run_log("Stop requested - waiting for the current step to exit...")
    return (dash.no_update,)


def _render_checklist():
    icons = {"pending": "☐", "running": "▶", "done": "☑"}
    colors = {"pending": "text-muted", "running": "text-primary fw-semibold", "done": "text-success"}
    steps = _run_state.get("steps") or DECAY_STEPS
    status = _run_state.get("step_status", {})
    return [
        html.Li(f"{icons[status.get(s, 'pending')]} {s}", className=colors[status.get(s, "pending")])
        for s in steps
    ]


def _format_duration(seconds):
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _solver_progress_text():
    """'Solver time' line for the Processing tab: current/target within
    the phase currently running (a flow-convergence chunk, a steady-state
    phase, or the pimpleFoam decay run - see _PHASE_TARGET_PATTERNS) plus
    an ETA extrapolated from how fast Time has advanced since that phase
    started, not the whole run's elapsed time (an earlier phase's pace
    would otherwise skew the estimate).
    """
    cur = _run_state.get("current_time")
    if not cur:
        return ""
    try:
        cur_val = float(cur)
    except (TypeError, ValueError):
        return f"Solver time: {cur}"

    target = _run_state.get("target_time")
    phase_start = _run_state.get("phase_start_time")
    if not target or not phase_start:
        return f"Solver time: {cur_val:.4g}"

    pct = min(100, round(100 * cur_val / target))
    text = f"Solver time: {cur_val:.4g} / {target:.4g} ({pct}%)"
    elapsed = time.time() - phase_start
    if cur_val > 0 and elapsed > 0:
        rate = cur_val / elapsed
        if rate > 0:
            text += f" — ETA ~{_format_duration((target - cur_val) / rate)}"
    return text


@app.callback(
    Output("run-log", "children"),
    Output("run-status-text", "children"),
    Output("run-btn", "disabled", allow_duplicate=True),
    Output("continue-btn", "disabled", allow_duplicate=True),
    Output("run-poll", "disabled"),
    Output("stop-btn", "disabled"),
    Output("run-checklist", "children"),
    Output("run-progress-bar", "value"),
    Output("run-progress-bar", "label"),
    Output("run-elapsed", "children"),
    Output("run-current-time", "children"),
    Output("results-data", "data", allow_duplicate=True),
    Input("run-poll", "n_intervals"),
    prevent_initial_call=True,
)
def _poll_run(n_intervals):
    status = _run_state["status"]
    log_text = "\n".join(_run_state["log"][-300:])
    status_text = {
        "running": "Running...",
        "done": "Finished.",
        "error": "Failed - see log below.",
        "stopped": "Stopped.",
    }.get(status, "")
    still_running = status == "running"

    steps = _run_state.get("steps") or []
    step_status = _run_state.get("step_status", {})
    n_done = sum(1 for s in steps if step_status.get(s) == "done")
    pct = round(100 * n_done / len(steps)) if steps else 0

    start = _run_state.get("start_time")
    elapsed = f"Elapsed: {int(time.time() - start)}s" if start else ""
    cur_time_text = _solver_progress_text()

    # Auto-load this run's own results once it finishes, so the Analysis
    # tab has something to show without a separate manual step - polling
    # stops right after this (run-poll.disabled becomes True), so this
    # only fires once, exactly when status first becomes "done".
    results_data = dash.no_update
    if status == "done" and _run_state.get("case_dir"):
        try:
            with open(f"{_run_state['case_dir']}/results.json") as f:
                results_data = json.load(f)
        except Exception:
            results_data = dash.no_update

    return (log_text, status_text, still_running, still_running, not still_running, not still_running,
            _render_checklist(), pct, f"{pct}%", elapsed, cur_time_text, results_data)


@app.callback(
    Output("preview-graph", "figure"),
    Input("project-status", "children"),
    Input("inlet-show", "value"), Input("inlet-wall", "value"),
    Input("inlet-y-input", "value"), Input("inlet-z-input", "value"),
    Input("inlet-size-w", "value"), Input("inlet-size-h", "value"),
    Input("outlet-show", "value"), Input("outlet-wall", "value"),
    Input("outlet-y-input", "value"), Input("outlet-z-input", "value"),
    Input("outlet-size-w", "value"), Input("outlet-size-h", "value"),
    Input("fan-enable", "value"), Input("fan-speed", "value"), Input("fan-direction", "value"),
    Input("fan-radius", "value"), Input("fan-thickness", "value"),
    Input("fan-x-input", "value"), Input("fan-y-input", "value"), Input("fan-z-input", "value"),
    Input("sim-type", "value"),
    Input("inject-x-input", "value"), Input("inject-y-input", "value"), Input("inject-z-input", "value"),
)
def _update_preview(_status, inlet_show, inlet_wall, inlet_y, inlet_z, inlet_w, inlet_h,
                     outlet_show, outlet_wall, outlet_y, outlet_z, outlet_w, outlet_h,
                     fan_enable, fan_speed, fan_direction, fan_radius, fan_thickness,
                     fan_x, fan_y, fan_z, sim_type, inject_x, inject_y, inject_z):
    room = _loaded["room"]
    if room is None:
        return _empty_preview_figure()

    inlet_center = (inlet_y / room.y, inlet_z / room.z)
    outlet_center = (outlet_y / room.y, outlet_z / room.z)

    fan_kwargs = {}
    if fan_enable:
        direction = (0, 0, -1) if fan_direction == "down" else (0, 0, 1)
        fan_kwargs = dict(
            fan_speed=fan_speed, fan_disk_radius=fan_radius, fan_disk_thickness=fan_thickness,
            fan_center=(fan_x, fan_y, fan_z), fan_direction=direction,
        )

    injection_center = (inject_x, inject_y, inject_z) if sim_type == "steady_state" else None

    fig = plot_case(
        room,
        inlet_wall=inlet_wall, inlet_center=inlet_center, inlet_size=(inlet_w, inlet_h),
        outlet_wall=outlet_wall, outlet_center=outlet_center, outlet_size=(outlet_w, outlet_h),
        injection_center=injection_center,
        title="", **fan_kwargs,
    )
    if not inlet_show:
        fig.data = [t for t in fig.data if not (t.customdata and str(t.customdata[0]).startswith("inlet"))]
    if not outlet_show:
        fig.data = [t for t in fig.data if not (t.customdata and str(t.customdata[0]).startswith("outlet"))]
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0))
    return fig


if __name__ == "__main__":
    # use_reloader=False: Werkzeug's reloader re-execs this module in a
    # subprocess, which crashes here (likely the tkinter import or the
    # WSL subprocess call in _compute_default_run_dir() re-running in the
    # forked child) - verified by reproducing with/without it.
    app.run(debug=True, use_reloader=False)
