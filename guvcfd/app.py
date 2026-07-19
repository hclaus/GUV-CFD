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
from datetime import datetime
from pathlib import Path
from tkinter import filedialog

import dash
import dash_bootstrap_components as dbc
import numpy as np
import plotly.graph_objs as go
from dash import Input, Output, State, dcc, html
from guv_calcs import Project

from .app_settings import ADVANCED_SETTINGS_DEFAULTS, load_advanced_settings, save_advanced_settings
from .case_io import clear_stale_run_output, read_cell_centers
from .decay_analysis import write_results_summary
from .fan import fan_fvoptions_entry
from .fluence import compute_fluence_at_points, compute_inactivation_rate, compute_well_mixed_eACH
from . import help_content
from .initial_fields import compute_inlet_velocities
from .monitoring_points import compute_monitoring_results, mixing_uniformity_note
from .paraview_launch import launch_paraview
from .report import generate_report_docx, T_FIELD_NOTE, EFFECTIVE_ACH_NOTE, _phase_ss_rows, _ach_source_note
from .result_figures import steady_state_figure, decay_figure
from .run_pipeline import setup_case
from . import scenario_runs
from .splice import set_control_dict_start_from, set_control_dict_time
from .steady_state_pipeline import run_steady_state_scenario
from .ventilation_control import run_ventilation_only_control
from .visualization import WALL_POSITION_DIMS, center_frac_for_wall, plot_case
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

WALL_OPTIONS = [{"label": w, "value": w} for w in
                ("xMin", "xMax", "frontWall", "backWall", "floor", "ceiling")]

# Every plain-value form field that a GUV-CFD project file (.guvcfd, JSON)
# saves/restores. Position fields use their "-input" id, not "-slider" -
# the slider is kept in sync from it (see _register_position_field), so
# only the number box needs to round-trip.
SETTINGS_FIELDS = [
    "project-description", "case-dir", "ach", "z-value",
    "inlet-show", "inlet-wall", "inlet-y-input", "inlet-z-input", "inlet-size-w", "inlet-size-h",
    "inlet-diffuser-type",
    "outlet-show", "outlet-wall", "outlet-y-input", "outlet-z-input", "outlet-size-w", "outlet-size-h",
    "inlet2-enable", "inlet2-wall", "inlet2-y-input", "inlet2-z-input", "inlet2-size-w", "inlet2-size-h",
    "inlet2-diffuser-type",
    "outlet2-enable", "outlet2-wall", "outlet2-y-input", "outlet2-z-input", "outlet2-size-w", "outlet2-size-h",
    "fan-enable", "fan-speed", "fan-direction", "fan-radius", "fan-thickness",
    "fan-x-input", "fan-y-input", "fan-z-input",
    "sim-type", "pimple-end-time", "pimple-write-interval", "no-uv-control-enable",
    "target-t-ss", "inject-x-input", "inject-y-input", "inject-z-input",
    "phase1-iterations", "phase2-iterations", "t-ss-window-frac",
    "monitoring-enable",
    "monitor1-enable", "monitor1-name", "monitor1-x-input", "monitor1-y-input",
    "monitor1-z-input", "monitor1-cells",
    "monitor2-enable", "monitor2-name", "monitor2-x-input", "monitor2-y-input",
    "monitor2-z-input", "monitor2-cells",
    "monitor3-enable", "monitor3-name", "monitor3-x-input", "monitor3-y-input",
    "monitor3-z-input", "monitor3-cells",
]

MONITOR_POINT_IDS = [1, 2, 3]

# Position-field spec: (prefix, label, room-dimension attr for the slider's
# max, default-value function of room, initial default/min/max/step used
# before any project is loaded). Shared by inlet/outlet, fan, and injection
# controls so their slider<->number sync + "reset to room" callbacks can be
# registered in one loop instead of duplicated per field.
POSITION_FIELDS = [
    # Labels are generic ("Position 1/2") rather than wall-specific ("Across-
    # wall Y"/"Height Z") since these openings can now be on any of the 6
    # room walls (not just xMin/xMax) - each opening's own wall dropdown,
    # right above its position fields, gives the needed context instead.
    ("inlet-y", "Position 1 (m)", "y", lambda r: r.y / 2, 1.5, 0, 10, 0.05),
    ("inlet-z", "Position 2 (m)", "z", lambda r: 0.85 * r.z, 2.1, 0, 5, 0.05),
    ("outlet-y", "Position 1 (m)", "y", lambda r: r.y / 2, 1.5, 0, 10, 0.05),
    ("outlet-z", "Position 2 (m)", "z", lambda r: 0.15 * r.z, 0.4, 0, 5, 0.05),
    ("inlet2-y", "Position 1 (m)", "x", lambda r: r.x / 2, 2.0, 0, 10, 0.05),
    ("inlet2-z", "Position 2 (m)", "y", lambda r: r.y / 2, 1.5, 0, 10, 0.05),
    ("outlet2-y", "Position 1 (m)", "x", lambda r: r.x / 2, 2.0, 0, 10, 0.05),
    ("outlet2-z", "Position 2 (m)", "y", lambda r: r.y / 2, 1.5, 0, 10, 0.05),
    ("fan-x", "X position (m)", "x", lambda r: r.x / 2, 2.0, 0, 10, 0.05),
    ("fan-y", "Y position (m)", "y", lambda r: r.y / 2, 1.5, 0, 10, 0.05),
    ("fan-z", "Height — Z (m)", "z", lambda r: max(r.z - 0.3, 0), 2.2, 0, 5, 0.05),
    ("inject-x", "X position (m)", "x", lambda r: r.x / 2, 2.0, 0, 10, 0.05),
    ("inject-y", "Y position (m)", "y", lambda r: r.y / 2, 1.5, 0, 10, 0.05),
    ("inject-z", "Height — Z (m)", "z", lambda r: min(1.5, r.z), 1.5, 0, 5, 0.05),
    ("monitor1-x", "X position (m)", "x", lambda r: r.x / 2, 2.0, 0, 10, 0.05),
    ("monitor1-y", "Y position (m)", "y", lambda r: r.y / 2, 1.5, 0, 10, 0.05),
    ("monitor1-z", "Height — Z (m)", "z", lambda r: min(1.5, r.z), 1.5, 0, 5, 0.05),
    ("monitor2-x", "X position (m)", "x", lambda r: 0.75 * r.x, 3.0, 0, 10, 0.05),
    ("monitor2-y", "Y position (m)", "y", lambda r: r.y / 2, 1.5, 0, 10, 0.05),
    ("monitor2-z", "Height — Z (m)", "z", lambda r: min(1.5, r.z), 1.5, 0, 5, 0.05),
    ("monitor3-x", "X position (m)", "x", lambda r: 0.25 * r.x, 1.0, 0, 10, 0.05),
    ("monitor3-y", "Y position (m)", "y", lambda r: r.y / 2, 1.5, 0, 10, 0.05),
    ("monitor3-z", "Height — Z (m)", "z", lambda r: min(1.5, r.z), 1.5, 0, 5, 0.05),
]
_POSITION_FIELD_BY_PREFIX = {f[0]: f for f in POSITION_FIELDS}


def _compute_default_run_dir():
    """Ask WSL for OpenFOAM's own $FOAM_RUN convention and create it if
    missing, so the GUI's default project directory is a real, usable path
    rather than a guess. Returns a \\\\wsl.localhost\\... UNC path (browsable
    from Windows); wsl_utils.wsl_path() converts it back for subprocess use.
    """
    try:
        r = run_wsl('mkdir -p "$FOAM_RUN"; printf "%s|%s" "$WSL_DISTRO_NAME" "$FOAM_RUN"', "$HOME")
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


def _parse_number_list(text):
    """"2, 6, 6.5" -> [2.0, 6.0, 6.5]; [] for empty/whitespace-only input.
    Raises ValueError (with the offending token) on anything that doesn't
    parse as a number - callers turn that into a user-facing message.
    """
    if not text or not text.strip():
        return []
    values = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            values.append(float(part))
        except ValueError:
            raise ValueError(f"'{part}' is not a number")
    return values


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

# Scenario Runs (Z x ACH sweep) state - deliberately its own dict rather
# than reusing _run_state, so a sweep and a normal single Run never cross
# wires (e.g. the Processing tab's Stop button must never abort a sweep,
# and vice versa). "results" is keyed by (z, ach) -> {"status": "done"/
# "error", "detail": trimmed result dict or an error message}.
_scenario_state = {
    "status": "idle", "log": [], "combos": [], "results": {},
    "start_time": None, "stop_requested": False,
}


def _scenario_log(msg):
    msg = str(msg)
    log = _scenario_state["log"]
    log.append(msg)
    if len(log) > _MAX_LOG_LINES:
        del log[: len(log) - _MAX_LOG_LINES]


def _scenario_should_stop():
    return _scenario_state.get("stop_requested", False)

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

    steps = _run_state.get("steps", [])
    for substr, step_name in _run_state.get("markers", []):
        if substr in msg and step_name in steps:
            idx = steps.index(step_name)
            for i, s in enumerate(steps):
                _run_state["step_status"][s] = "done" if i < idx else "running" if i == idx else \
                    _run_state["step_status"].get(s, "pending")
            break


def _track_solver_time(line):
    """on_line callback for a solver's (simpleFoam/pimpleFoam) raw stdout -
    updates the live "Solver time: X/Y - ETA" indicator from "Time = N"
    lines without appending anything to the visible run log. OpenFOAM
    prints several residual/continuity-error lines per iteration; over a
    multi-thousand-iteration run, appending all of it (the old behavior -
    on_line was just _run_log) flooded the kept log fast enough to scroll
    real narration (step transitions, convergence summaries, errors) out
    of the visible window within seconds of the next step starting.
    """
    m = _TIME_RE.match(line.strip())
    if m:
        base = _run_state.get("chunk_base")
        _run_state["current_time"] = str(float(m.group(1)) + base) if base is not None else m.group(1)


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


# Re-exported under their old private names - moved to visualization.py so
# report.py can reuse them too without a circular import back into app.py.
_center_frac_for_wall = center_frac_for_wall


def _opening_center_frac(settings, prefix, room):
    """(c1, c2) fractions for setup_case()'s inlet_center/outlet_center."""
    return _center_frac_for_wall(settings[f"{prefix}-wall"], settings[f"{prefix}-y-input"],
                                  settings[f"{prefix}-z-input"], room)


def _second_opening_kwargs(settings, prefix, room):
    """setup_case()'s inlet2_*/outlet2_* kwargs for a 2nd inlet/outlet -
    {} when its own enable toggle is off, matching setup_case()'s "no 2nd
    opening" default (same shape as _fan_kwargs).
    """
    if not settings.get(f"{prefix}-enable"):
        return {}
    kwargs = {
        f"{prefix}_wall": settings[f"{prefix}-wall"],
        f"{prefix}_center": _opening_center_frac(settings, prefix, room),
        f"{prefix}_size": (settings[f"{prefix}-size-w"], settings[f"{prefix}-size-h"]),
    }
    if prefix == "inlet2":  # only inlets have a diffuser type, not outlets
        kwargs["inlet2_diffuser_type"] = settings.get("inlet2-diffuser-type", "direct")
    return kwargs


# Settings that determine the mesh/flow field/UV zones a full Run builds -
# everything Continue reuses as-is without regenerating. If any of these
# differ between what's on disk and what the GUI currently shows, Continue
# would silently apply the OLD values (not what the user now sees in the
# form) since it only touches pimpleFoam. pimple-end-time/write-interval are
# deliberately excluded - changing those is the whole point of Continue.
_MESH_AFFECTING_FIELDS = [
    "ach", "z-value",
    "inlet-wall", "inlet-y-input", "inlet-z-input", "inlet-size-w", "inlet-size-h",
    # Doesn't change the mesh itself, but does change the converged flow
    # field's boundary values - Continue reusing a flow field solved under
    # the OLD diffuser type would silently keep using it, so this needs
    # the same mismatch-detection treatment as genuinely mesh-affecting
    # fields (same reasoning as ach/inlet position above).
    "inlet-diffuser-type",
    "outlet-wall", "outlet-y-input", "outlet-z-input", "outlet-size-w", "outlet-size-h",
    # Unlike monitoring points/source_center below, a 2nd inlet/outlet
    # genuinely changes the mesh (an extra carved patch) - these belong
    # here, not in the "purely informational" bucket.
    "inlet2-enable", "inlet2-wall", "inlet2-y-input", "inlet2-z-input",
    "inlet2-size-w", "inlet2-size-h", "inlet2-diffuser-type",
    "outlet2-enable", "outlet2-wall", "outlet2-y-input", "outlet2-z-input",
    "outlet2-size-w", "outlet2-size-h",
    "fan-enable", "fan-speed", "fan-direction", "fan-radius", "fan-thickness",
    "fan-x-input", "fan-y-input", "fan-z-input",
]


def _save_run_settings(case_dir, settings, guv_path=None):
    data = {k: settings.get(k) for k in _MESH_AFFECTING_FIELDS}
    # guv_path is provenance for report generation (reloading the Room to
    # render a preview image) - not compared by _settings_mismatch, which
    # only ever iterates _MESH_AFFECTING_FIELDS.
    if guv_path is not None:
        data["guv_path"] = guv_path
    # The currently-open .guvcfd project file (if any) - provenance for the
    # report's "Project file:" line, same idea as guv_path above.
    data["settings_path"] = _loaded.get("settings_path")
    # Monitoring points don't affect the mesh/flow field (pure post-
    # processing - see monitoring_points.py's module docstring), so they're
    # deliberately not in _MESH_AFFECTING_FIELDS and never trigger a
    # Continue mismatch warning. Saved here anyway, under their own key,
    # purely so report.py's case-setup preview picture can draw them later
    # without needing the original .guv/Dash session still open.
    data["monitoring_points"] = _gather_monitoring_points(settings)
    # Same idea for the steady-state contaminant source position - carved as
    # its own cellZone independent of setup_case()'s mesh (see
    # steady_state_pipeline.run_steady_state_scenario), so it's not mesh-
    # affecting either, but paraview_launch needs it later to seed a
    # source-colored-by-T view. None for decay scenarios, which have no
    # continuous point source.
    if settings.get("sim-type") == "steady_state":
        data["source_center"] = (
            settings.get("inject-x-input"), settings.get("inject-y-input"),
            settings.get("inject-z-input"),
        )
    with open(f"{case_dir}/run_settings.json", "w") as f:
        json.dump(data, f, indent=2)


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


# Fields a Run always needs a real numeric value for. Checked upfront so a
# missing value (a cleared number input, or an older/hand-edited .guvcfd
# file predating a field - e.g. "z-value": null) fails fast with a clear
# message, instead of after mesh generation and flow convergence have
# already run for real and the pipeline reaches the one step that actually
# needs it (compute_inactivation_rate needing Z, notably, only happens near
# the very end).
_ALWAYS_REQUIRED_FIELDS = {
    "ach": "Ventilation ACH", "z-value": "UV inactivation constant Z",
    "inlet-y-input": "Inlet Y position", "inlet-z-input": "Inlet Z position",
    "inlet-size-w": "Inlet width", "inlet-size-h": "Inlet height",
    "outlet-y-input": "Outlet Y position", "outlet-z-input": "Outlet Z position",
    "outlet-size-w": "Outlet width", "outlet-size-h": "Outlet height",
    "pimple-end-time": "Simulation end time", "pimple-write-interval": "Write interval",
}
_FAN_REQUIRED_FIELDS = {
    "fan-speed": "Fan speed", "fan-radius": "Fan radius", "fan-thickness": "Fan thickness",
    "fan-x-input": "Fan X position", "fan-y-input": "Fan Y position", "fan-z-input": "Fan Z position",
}
_INLET2_REQUIRED_FIELDS = {
    "inlet2-y-input": "2nd inlet Y position", "inlet2-z-input": "2nd inlet Z position",
    "inlet2-size-w": "2nd inlet width", "inlet2-size-h": "2nd inlet height",
}
_OUTLET2_REQUIRED_FIELDS = {
    "outlet2-y-input": "2nd outlet Y position", "outlet2-z-input": "2nd outlet Z position",
    "outlet2-size-w": "2nd outlet width", "outlet2-size-h": "2nd outlet height",
}
_STEADY_STATE_REQUIRED_FIELDS = {
    "target-t-ss": "Target steady-state T",
    "inject-x-input": "Injection X position", "inject-y-input": "Injection Y position",
    "inject-z-input": "Injection Z position",
    "phase1-iterations": "Phase 1 iterations", "phase2-iterations": "Phase 2 iterations",
}


def _validate_settings(settings):
    """Labels of any required-but-missing (None) field, given the current
    sim-type/fan/monitoring toggles. [] if everything a Run would touch is
    present.
    """
    required = dict(_ALWAYS_REQUIRED_FIELDS)
    if settings.get("fan-enable"):
        required.update(_FAN_REQUIRED_FIELDS)
    if settings.get("inlet2-enable"):
        required.update(_INLET2_REQUIRED_FIELDS)
    if settings.get("outlet2-enable"):
        required.update(_OUTLET2_REQUIRED_FIELDS)
    if settings.get("sim-type") == "steady_state":
        required.update(_STEADY_STATE_REQUIRED_FIELDS)
    if settings.get("monitoring-enable"):
        for i in MONITOR_POINT_IDS:
            if not settings.get(f"monitor{i}-enable"):
                continue
            label = settings.get(f"monitor{i}-name") or f"Point {i}"
            required[f"monitor{i}-x-input"] = f"{label} X position"
            required[f"monitor{i}-y-input"] = f"{label} Y position"
            required[f"monitor{i}-z-input"] = f"{label} Z position"
            required[f"monitor{i}-cells"] = f"{label} cells per side"
    return [label for field, label in required.items() if settings.get(field) is None]


def _gather_monitoring_points(settings):
    """Enabled monitoring points from settings, in the shape
    monitoring_points.compute_monitoring_results() expects. [] if the
    master "monitoring-enable" toggle is off, or no individual point is
    enabled under it.
    """
    if not settings.get("monitoring-enable"):
        return []
    points = []
    for i in MONITOR_POINT_IDS:
        if not settings.get(f"monitor{i}-enable"):
            continue
        points.append({
            "name": settings.get(f"monitor{i}-name") or f"Point {i}",
            "x": settings[f"monitor{i}-x-input"],
            "y": settings[f"monitor{i}-y-input"],
            "z": settings[f"monitor{i}-z-input"],
            "cells_per_side": settings[f"monitor{i}-cells"],
        })
    return points


def _run_decay(guv_path, case_dir, room, settings):
    adv = load_advanced_settings()
    summary = setup_case(
        guv_path, case_dir, template_case_dir=TEMPLATE_CASE_DIR,
        Z=settings["z-value"], ach=settings["ach"],
        inlet_wall=settings["inlet-wall"],
        inlet_center=_opening_center_frac(settings, "inlet", room),
        inlet_size=(settings["inlet-size-w"], settings["inlet-size-h"]),
        inlet_diffuser_type=settings.get("inlet-diffuser-type", "direct"),
        outlet_wall=settings["outlet-wall"],
        outlet_center=_opening_center_frac(settings, "outlet", room),
        outlet_size=(settings["outlet-size-w"], settings["outlet-size-h"]),
        pimple_end_time=settings["pimple-end-time"],
        pimple_write_interval=settings["pimple-write-interval"],
        pimple_delta_t=adv["pimple-delta-t"],
        cell_size=adv["mesh-cell-size"], nbins=adv["uv-zone-bins"],
        flow_rel_tol=adv["flow-rel-tol"] / 100.0, flow_max_iterations=adv["flow-max-iterations"],
        momentum_relaxation=adv["momentum-relaxation"], scalar_relaxation=adv["scalar-relaxation"],
        log_fn=_run_log, should_stop=_should_stop, solver_log_fn=_track_solver_time,
        **_fan_kwargs(settings),
        **_second_opening_kwargs(settings, "inlet2", room),
        **_second_opening_kwargs(settings, "outlet2", room),
    )
    if _should_stop():
        raise StoppedByUser("Stopped after case setup.")

    # Record what the mesh/flow field were actually built with, regardless
    # of whether pimpleFoam below succeeds - Continue compares against this,
    # not against whatever the GUI happens to show later.
    _save_run_settings(case_dir, settings, guv_path=guv_path)

    case_dir_wsl = wsl_path(case_dir)
    _run_log(f"Running pimpleFoam to {settings['pimple-end-time']}s (this can take a while)...")
    r = run_wsl_streaming(
        "pimpleFoam 2>&1 | tee log.pimpleFoam", case_dir_wsl,
        on_line=_track_solver_time, should_stop=_should_stop, kill_pattern="pimpleFoam",
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
        summary["eACH_uv_well_mixed_mean"], extra={"n_lamps": summary["n_lamps"], "fluence_mean": summary["fluence_mean"]},
    )

    if settings.get("no-uv-control-enable"):
        if _should_stop():
            raise StoppedByUser("Stopped before UV-off control run.")
        _run_log("=== Running UV-off ventilation-only control (subfolder \"no_UV\") ===")
        control_results = run_ventilation_only_control(
            case_dir, f"{case_dir}/no_UV", settings["ach"], room.x, room.y, room.z,
            settings["inlet-wall"], (settings["inlet-size-w"], settings["inlet-size-h"]),
            settings["pimple-end-time"], settings["pimple-write-interval"],
            inlet2_wall=settings["inlet2-wall"] if settings.get("inlet2-enable") else None,
            inlet2_size=(settings["inlet2-size-w"], settings["inlet2-size-h"])
            if settings.get("inlet2-enable") else None,
            has_outlet2=bool(settings.get("outlet2-enable")),
            log_fn=_run_log, should_stop=_should_stop, solver_log_fn=_track_solver_time,
        )
        _run_log("Updating results.json with corrected mixing efficiency (measured, "
                 "not nominal, ventilation ACH)...")
        results = write_results_summary(
            case_dir, f"{case_dir}/results.json", settings["ach"],
            summary["eACH_uv_well_mixed_mean"], extra={"n_lamps": summary["n_lamps"], "fluence_mean": summary["fluence_mean"]},
            measured_ventilation_ach=control_results["total_ach_effective"],
        )

    points = _gather_monitoring_points(settings)
    if points:
        if _should_stop():
            raise StoppedByUser("Stopped before monitoring locations.")
        _run_log("=== Computing monitoring locations ===")
        results["monitoring"] = compute_monitoring_results(
            case_dir, points, cell_size=adv["mesh-cell-size"],
            ventilation_ach=settings["ach"], log_fn=_run_log)
        with open(f"{case_dir}/results.json", "w") as f:
            json.dump(results, f, indent=2)

    _complete_all_steps()
    _run_log(f"Done. eACH_uv effective={results['eACH_uv_effective']:.4g} /hr "
             f"(well-mixed={results['eACH_uv_well_mixed']:.4g} /hr)")
    if "mixing_efficiency_corrected" in results:
        _run_log(f"  Corrected mixing efficiency (measured ventilation baseline): "
                 f"{results['mixing_efficiency_corrected'] * 100:.1f}% "
                 f"(vs {results['mixing_efficiency'] * 100:.1f}% using nominal ACH)")


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
        on_line=_track_solver_time, should_stop=_should_stop, kill_pattern="pimpleFoam",
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


def _estimate_well_mixed_eACH(room, z_value, grid_n=(10, 8, 8)):
    """Quick well-mixed eACH_UV estimate straight from the room/lamps/Z - no
    CFD mesh needed, since compute_fluence_at_points works on any point
    cloud (see fluence.py). Used only to prefill a sensible suggested
    simulated duration / settling time before any OpenFOAM run exists; a
    coarse grid is fine since this is a starting suggestion the user can
    always override, not a final result.
    """
    nx, ny, nz = grid_n
    xs = np.linspace(0, room.x, nx + 2)[1:-1]
    ys = np.linspace(0, room.y, ny + 2)[1:-1]
    zs = np.linspace(0, room.z, nz + 2)[1:-1]
    grid = np.stack(np.meshgrid(xs, ys, zs, indexing="ij"), axis=-1).reshape(-1, 3)
    values = compute_fluence_at_points(room, grid)
    k_values = compute_inactivation_rate(values, z_value)
    return float(compute_well_mixed_eACH(k_values).mean())


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
    adv = load_advanced_settings()
    fan_kwargs = _fan_kwargs(settings)

    _run_log("=== Setting up mesh, flow field, and UV zones ===")
    summary = setup_case(
        guv_path, case_dir, template_case_dir=TEMPLATE_CASE_DIR,
        Z=settings["z-value"], ach=settings["ach"],
        inlet_wall=settings["inlet-wall"],
        inlet_center=_opening_center_frac(settings, "inlet", room),
        inlet_size=(settings["inlet-size-w"], settings["inlet-size-h"]),
        inlet_diffuser_type=settings.get("inlet-diffuser-type", "direct"),
        outlet_wall=settings["outlet-wall"],
        outlet_center=_opening_center_frac(settings, "outlet", room),
        outlet_size=(settings["outlet-size-w"], settings["outlet-size-h"]),
        cell_size=adv["mesh-cell-size"], nbins=adv["uv-zone-bins"],
        flow_rel_tol=adv["flow-rel-tol"] / 100.0, flow_max_iterations=adv["flow-max-iterations"],
        momentum_relaxation=adv["momentum-relaxation"], scalar_relaxation=adv["scalar-relaxation"],
        log_fn=_run_log, should_stop=_should_stop, solver_log_fn=_track_solver_time,
        **fan_kwargs,
        **_second_opening_kwargs(settings, "inlet2", room),
        **_second_opening_kwargs(settings, "outlet2", room),
    )
    if _should_stop():
        raise StoppedByUser("Stopped after case setup.")

    # Same record _run_decay writes - Continue's settings-mismatch check and
    # the .docx report generator both need it regardless of scenario type.
    _save_run_settings(case_dir, settings, guv_path=guv_path)

    fan_entry = None
    if settings["fan-enable"]:
        fan_entry = fan_fvoptions_entry(settings["fan-speed"], direction=fan_kwargs["fan_direction"])

    room_volume = room.x * room.y * room.z
    openings = [(settings["inlet-wall"], settings["inlet-size-w"] * settings["inlet-size-h"])]
    has_inlet2 = bool(settings.get("inlet2-enable"))
    if has_inlet2:
        openings.append((settings["inlet2-wall"], settings["inlet2-size-w"] * settings["inlet2-size-h"]))
    velocities = compute_inlet_velocities(settings["ach"], room_volume, openings)
    inlet_velocity = velocities[0]
    inlet2_velocity = velocities[1] if has_inlet2 else None
    has_outlet2 = bool(settings.get("outlet2-enable"))

    ach = settings["ach"]
    eACH_uv = summary.get("eACH_uv_well_mixed_mean", 0.0)
    phase1_iterations = max(settings["phase1-iterations"], _settling_iterations(ach))
    phase2_iterations = max(settings["phase2-iterations"], _settling_iterations(ach + eACH_uv))
    _run_log(f"99.5% settling estimate: phase1={_settling_iterations(ach)} iterations "
             f"(ACH={ach:.3g}/hr alone), phase2={_settling_iterations(ach + eACH_uv)} iterations "
             f"(ACH+eACH_uv={ach + eACH_uv:.3g}/hr) - using the larger of this and the configured "
             f"value for each phase ({phase1_iterations}, {phase2_iterations}).")

    patches_to_monitor = ("outlet", "outlet2") if has_outlet2 else ("outlet",)
    result = run_steady_state_scenario(
        case_dir, room.x, room.y, room.z, settings["ach"], settings["z-value"],
        source_center=(settings["inject-x-input"], settings["inject-y-input"], settings["inject-z-input"]),
        target_T_ss=settings["target-t-ss"],
        inlet_velocity=inlet_velocity, inlet2_velocity=inlet2_velocity, has_outlet2=has_outlet2,
        inlet_diffuser_type=settings.get("inlet-diffuser-type", "direct"),
        inlet_wall=settings["inlet-wall"], inlet_center=_opening_center_frac(settings, "inlet", room),
        inlet_size=(settings["inlet-size-w"], settings["inlet-size-h"]),
        inlet2_diffuser_type=settings.get("inlet2-diffuser-type", "direct") if has_inlet2 else "direct",
        inlet2_wall=settings["inlet2-wall"] if has_inlet2 else None,
        inlet2_center=_opening_center_frac(settings, "inlet2", room) if has_inlet2 else None,
        inlet2_size=(settings["inlet2-size-w"], settings["inlet2-size-h"]) if has_inlet2 else None,
        phase1_iterations=phase1_iterations,
        phase2_iterations=phase2_iterations,
        window_frac=settings.get("t-ss-window-frac") or 0.15,
        cell_size=adv["mesh-cell-size"], nbins=adv["uv-zone-bins"],
        source_size=adv["source-zone-size"],
        plateau_rel_tol=adv["plateau-rel-tol"] / 100.0,
        # 500-iteration check interval: the value backtested against a
        # real run (see check_t_infinity_stability's docstring) - only
        # meaningful when t_inf_rel_tol is actually set below, since
        # _run_phase defaults check_interval to the whole phase (a no-op
        # single chunk) otherwise.
        t_inf_check_interval=500 if adv["t-infinity-early-stop-enabled"] else None,
        t_inf_rel_tol=(adv["t-infinity-rel-tol"] / 100.0) if adv["t-infinity-early-stop-enabled"] else None,
        keep_all_timesteps=adv["keep-all-timesteps"],
        fan_entry=fan_entry, monitoring_points=_gather_monitoring_points(settings),
        patches_to_monitor=patches_to_monitor,
        log_fn=_run_log, should_stop=_should_stop, solver_log_fn=_track_solver_time,
    )
    result["fluence_mean"] = summary["fluence_mean"]
    result["eACH_uv_well_mixed"] = summary.get("eACH_uv_well_mixed_mean")
    with open(f"{case_dir}/results.json", "w") as f:
        json.dump(result, f, indent=2)
    _complete_all_steps()
    _run_log(f"Done. Reduction={result['reduction_pct']:.1f}%, "
             f"eACH_uv={result['eACH_uv_steady_state']:.4g} /hr")


def _record_run_timing(case_dir, started_at, elapsed_seconds):
    """Add run_started_at/run_elapsed_seconds to results.json after a
    successful run - report.py reads these for the "Simulation date"/
    "Total elapsed time" report rows. A no-op if results.json somehow
    isn't there (shouldn't happen after a "done" status, but this is purely
    informational, not worth failing the run over).
    """
    results_path = f"{case_dir}/results.json"
    if not Path(results_path).exists():
        return
    with open(results_path) as f:
        results = json.load(f)
    results["run_started_at"] = started_at.isoformat()
    results["run_elapsed_seconds"] = elapsed_seconds
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)


def _run_pipeline_thread(sim_type, guv_path, case_dir, room, settings):
    started_at = datetime.now()
    start = time.time()
    try:
        if sim_type == "decay":
            _run_decay(guv_path, case_dir, room, settings)
        else:
            _run_steady_state(guv_path, case_dir, room, settings)
        _run_state["status"] = "done"
        _record_run_timing(case_dir, started_at, time.time() - start)
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


def _native_save_file(title, defaultextension, filetypes, initialfile=None):
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    kwargs = {"title": title, "defaultextension": defaultextension, "filetypes": filetypes}
    if initialfile:
        kwargs["initialfile"] = initialfile
    path = filedialog.asksaveasfilename(**kwargs)
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


def _settings_checkbox_field(field_id, label, tooltip, value):
    """Same visual row as _settings_field, but for a boolean toggle
    (dbc.Checkbox) instead of a numeric input - both expose a plain
    "value" property, so the generic _SETTINGS_FIELD_IDS/_KEYS-driven
    save/load/reset machinery (a simple zip + json.dump/load, which
    round-trips bool exactly as well as float) needs no special-casing
    to include this alongside the numeric fields.
    """
    icon_id = f"{field_id}-info"
    return dbc.Row([
        dbc.Col(html.Div([
            html.Span(label, className="small"),
            html.Span(" ⓘ", id=icon_id, className="text-muted", style={"cursor": "help"}),
            dbc.Tooltip(tooltip, target=icon_id, placement="top"),
        ]), width=8),
        dbc.Col(dbc.Checkbox(id=field_id, value=value), width=4, className="text-end"),
    ], align="center", className="mb-2 gx-2")


def _settings_field(field_id, label, tooltip, unit, value):
    """One row of the Settings modal: label + hover (i) explanation + a
    right-aligned numeric input with its unit shown inline next to it.
    """
    icon_id = f"{field_id}-info"
    return dbc.Row([
        dbc.Col(html.Div([
            html.Span(label, className="small"),
            html.Span(" ⓘ", id=icon_id, className="text-muted", style={"cursor": "help"}),
            dbc.Tooltip(tooltip, target=icon_id, placement="top"),
        ]), width=8),
        dbc.Col(dbc.InputGroup([
            dcc.Input(id=field_id, type="number", value=value,
                       className="form-control form-control-sm text-end"),
            dbc.InputGroupText(unit, className="small"),
        ], size="sm"), width=4),
    ], align="center", className="mb-2 gx-2")


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


DIFFUSER_TYPE_OPTIONS = [
    {"label": "Direct jet", "value": "direct"},
    {"label": "Surface-attached (ceiling/wall diffuser)", "value": "ceiling"},
]

_GRID_SNAP_NOTE = ("Position and size are automatically snapped to the mesh grid (cell size, "
                    "Settings menu) - the actual carved geometry may shift by up to half a cell "
                    "from the exact values entered here.")


def _opening_controls(prefix, default_wall, is_inlet=True):
    controls = [
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
        ]), help_text=_GRID_SNAP_NOTE),
    ]
    if is_inlet:
        controls.append(_labeled("Diffuser type", dcc.Dropdown(
            id=f"{prefix}-diffuser-type", options=DIFFUSER_TYPE_OPTIONS,
            value="direct", clearable=False),
            help_text="Direct jet: a single beam straight into the room. Surface-attached: "
                      "spreads radially along the wall/ceiling like a real diffuser - "
                      "validated for round/square ceiling, vortex, and grille types. "
                      "Currently opt-in while a numerical instability with certain opening "
                      "sizes/geometries is being root-caused - see CHANGELOG."))
    return controls


def _second_opening_controls(prefix, label, default_wall, is_inlet=True):
    """A 2nd inlet/outlet, off by default - same layout shape as
    _monitoring_point_controls' enable-toggle + collapsible sub-section.
    """
    return html.Div([
        dbc.Checkbox(id=f"{prefix}-enable", value=False, label=f"Enable 2nd {label}",
                     className="mb-2"),
        html.Div(id=f"{prefix}-controls", children=_opening_controls(prefix, default_wall, is_inlet=is_inlet)),
    ], className="mt-3 pt-3 border-top")


def _fan_position_controls():
    return [_position_field_component(p) for p in ("fan-x", "fan-y", "fan-z")]


def _injection_position_controls():
    return [_position_field_component(p) for p in ("inject-x", "inject-y", "inject-z")]


def _monitoring_point_controls(i):
    prefix = f"monitor{i}"
    return html.Div([
        dbc.Checkbox(id=f"{prefix}-enable", value=False, label=f"Enable Point {i}",
                     className="mb-2"),
        html.Div(id=f"{prefix}-controls", children=[
            _labeled("Name", dcc.Input(id=f"{prefix}-name", type="text", value=f"Point {i}",
                                        className="form-control form-control-sm")),
            *[_position_field_component(f"{prefix}-{axis}") for axis in ("x", "y", "z")],
            _labeled("Averaging box size (cells per side)", dcc.Input(
                id=f"{prefix}-cells", type="number", value=4, min=1, max=20, step=1,
                className="form-control form-control-sm"),
                help_text="Box side length = this many mesh cells (default cell size 0.1m, "
                          "so 4 -> a 0.4m cube)."),
        ]),
    ], className="mb-3 pb-2 border-bottom")


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
                dbc.Col(dcc.Textarea(
                    id="case-dir", value=_DEFAULT_RUN_DIR,
                    placeholder=r"\\wsl.localhost\Ubuntu\home\...\run",
                    style={"height": "60px", "resize": "vertical"},
                    className="form-control form-control-sm"), width=8),
                dbc.Col(dbc.Button("Browse...", id="browse-case-dir-btn", size="sm",
                                   color="secondary", className="w-100"), width=4),
            ], className="g-2")),
        ]),

        _card("Ventilation & UV", [
            _labeled("Air changes per hour (ACH)", dcc.Input(
                id="ach", type="number", value=3.0, min=0.1, max=20, step=0.1, debounce=True,
                className="form-control form-control-sm")),
            _labeled("Z — UV susceptibility (cm²/mJ)", dcc.Input(
                id="z-value", type="number", value=2.0, min=0.01, max=20, step="any", debounce=True,
                className="form-control form-control-sm")),
        ]),

        _card("Inlet", _opening_controls("inlet", "xMin")
              + [_second_opening_controls("inlet2", "Inlet", "ceiling")]),

        _card("Outlet", _opening_controls("outlet", "xMax", is_inlet=False)
              + [_second_opening_controls("outlet2", "Outlet", "floor", is_inlet=False)]),

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

        _card("Monitoring locations", [
            dbc.Checkbox(id="monitoring-enable", value=False,
                         label="Enable monitoring locations", className="mb-2"),
            html.Div(id="monitoring-controls", children=[
                _monitoring_point_controls(1),
                _monitoring_point_controls(2),
                _monitoring_point_controls(3),
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
                _labeled("Simulated duration (s)", dbc.Row([
                    dbc.Col(dcc.Input(
                        id="pimple-end-time", type="number", value=120, min=10, max=7200, step=10,
                        className="form-control form-control-sm"), width=8),
                    dbc.Col(dbc.Button("Suggest", id="suggest-duration-btn", size="sm",
                                       color="secondary", outline=True, className="w-100"), width=4),
                ], className="g-2"),
                    help_text="Estimated time to 99% reduction from ACH + well-mixed eACH_uv."),
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
                html.Div(_GRID_SNAP_NOTE, className="form-text small mb-2"),
                *_injection_position_controls(),
                _labeled("Phase 1 iterations (no UV)", dcc.Input(
                    id="phase1-iterations", type="number", value=8000, min=500, max=50000, step=500,
                    className="form-control form-control-sm")),
                _labeled("Phase 2 iterations (UV on)", dcc.Input(
                    id="phase2-iterations", type="number", value=3000, min=500, max=50000, step=500,
                    className="form-control form-control-sm")),
                dbc.Button("Suggest settling times (99.5%)", id="suggest-phases-btn", size="sm",
                           color="secondary", outline=True, className="w-100 mt-1"),
                _labeled("T_ss moving-average window (fraction of samples)", dcc.Input(
                    id="t-ss-window-frac", type="number", value=0.15, min=0.01, max=0.9, step=0.01,
                    className="form-control form-control-sm"),
                    help_text="Room-wide T and every monitoring point report a trailing-window "
                              "mean/CV over this fraction of the live per-iteration samples, "
                              "instead of a single last-sample read - see the live-volAverage "
                              "validation. 0.15 = last 15% of samples."),
            ]),
        ]),

        dbc.Checkbox(id="no-uv-control-enable", value=False,
                     label="Also run a UV-off control (subfolder \"no_UV\") for corrected "
                           "mixing efficiency",
                     className="mb-2"),
        dbc.Button("Run simulation", id="run-btn", color="success", className="w-100 mb-2"),
        dbc.Button("Continue to longer duration", id="continue-btn", color="secondary",
                   outline=True, className="w-100 mb-2"),
        html.Div(id="run-validation-msg", className="small text-danger text-center mb-4"),
    ], width=4, className="compact-panel", style={"maxHeight": "88vh", "overflowY": "auto"}),

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

# Scenario Runs: sweep the currently-configured steady-state project over
# a comma-separated Z list x ACH list (full cross-product), one subfolder
# per combination directly under the project's case-dir - see
# scenario_runs.py. Every other Project Setup field (inlet/outlet/fan/
# monitoring/iterations) is reused unchanged from whatever's currently
# configured there; only z-value/ach vary per combination.
scenario_tab = dbc.Row([
    dbc.Col([
        html.Div(
            "Runs the current project's steady-state setup once per Z x ACH "
            "combination (every Z with every ACH), each into its own subfolder "
            "under the project directory. The flow field is converged once per "
            "distinct ACH and reused for every Z at that ACH, so a longer Z list "
            "at a fixed ACH is much cheaper than it looks.",
            className="small text-muted mb-3",
        ),
        _labeled("Z values (comma-separated)",
                 dcc.Input(id="scenario-z-values", type="text", placeholder="e.g. 2, 6",
                           className="form-control form-control-sm")),
        _labeled("ACH values (comma-separated)",
                 dcc.Input(id="scenario-ach-values", type="text", placeholder="e.g. 3, 6",
                           className="form-control form-control-sm mt-2")),
        html.Div(id="scenario-combo-count", className="small text-muted mt-2 mb-3"),
        dbc.Button("Run Sweep", id="scenario-run-btn", color="success", className="w-100 mb-2"),
        dbc.Button("Stop Sweep", id="scenario-stop-btn", color="danger", size="sm",
                    className="mb-2", disabled=True),
        html.Div(id="scenario-validation-msg", className="small text-danger mb-2"),
        html.Div(id="scenario-status-text", className="fs-6 fw-semibold mb-2"),
        dcc.Interval(id="scenario-poll", interval=2000, n_intervals=0, disabled=True),
    ], width=4),
    dbc.Col([
        html.Div("Combinations", className="small fw-semibold text-uppercase mb-1"),
        html.Div(id="scenario-progress-table"),
        html.Div("Log", className="small fw-semibold text-uppercase mb-1 mt-3"),
        html.Pre(id="scenario-log", className="small", style={
            "height": "40vh", "overflowY": "auto", "fontSize": "11px",
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


def _monitoring_summary_rows(monitoring):
    """Extra Analysis-tab rows for monitoring locations, if any were
    computed. Handles both decay's shape
    ({name: {t_seconds, volAverage_T, eACH_uv_effective?}}) and
    steady-state's shape ({name: {phase1: {...}, phase2: {...}}}).
    """
    if not monitoring:
        return []
    rows = [("Monitoring locations", "")]
    for name, data in monitoring.items():
        if "phase1" in data:
            p1, p2 = data["phase1"], data["phase2"]
            # T_ss/T_ss_cv (trailing-window moving average, see
            # decay_analysis.windowed_stats) when present; falls back to the
            # old last-sample read for results.json predating live tracking.
            T1 = p1.get("T_ss", p1["volAverage_T"][-1] if p1["volAverage_T"] else None)
            T2 = p2.get("T_ss", p2["volAverage_T"][-1] if p2["volAverage_T"] else None)
            value = f"T_ss1={T1:.4g}, T_ss2={T2:.4g}" if T1 is not None and T2 else "n/a"
            cv1, cv2 = p1.get("T_ss_cv"), p2.get("T_ss_cv")
            if cv1 is not None or cv2 is not None:
                cv1_text = f"{cv1 * 100:.1f}%" if cv1 is not None else "n/a"
                cv2_text = f"{cv2 * 100:.1f}%" if cv2 is not None else "n/a"
                value += f" (CV1={cv1_text}, CV2={cv2_text})"
            if T1:
                value += f", reduction={(1 - T2 / T1) * 100:.1f}%"
        else:
            T_final = data["volAverage_T"][-1] if data["volAverage_T"] else None
            value = f"final volAverage(T)={T_final:.4g}" if T_final is not None else "n/a"
            if data.get("eACH_uv_effective") is not None:
                value += f", eACH_uv={data['eACH_uv_effective']:.4g}/hr"
        rows.append((f"  {name}", value))
    return rows


def _result_notes(result):
    """T-field explanation (always shown) plus a mixing-uniformity warning
    (only when monitoring points show the room isn't well mixed) - appended
    after the kv rows on both summary tabs.
    """
    notes = [T_FIELD_NOTE]
    if "phase1" in result and result.get("ventilation_ach_measured") is not None:
        notes.append(EFFECTIVE_ACH_NOTE)
    uniformity = mixing_uniformity_note(result)
    if uniformity:
        notes.append(uniformity)
    return [html.Div(note, className="mb-1 fst-italic text-muted small") for note in notes]


def _steady_state_summary(result):
    p1, p2 = result["phase1"], result["phase2"]
    rows = []
    if result.get("fluence_mean") is not None:
        rows.append(("Average fluence rate", f"{result['fluence_mean']:.4g} µW/cm²"))
    rows.append(("Target T_ss (design)", f"{result.get('target_T_ss', '?')}"))
    if result.get("injection_rate_total") is not None:
        rows.append(("Source injection rate (total, room-wide)",
                      f"{result['injection_rate_total']:.4g} T-units/s (see note below)"))
    rows += _phase_ss_rows(1, "no UV", p1)
    rows += _phase_ss_rows(2, "UV on", p2)
    ach_note = _ach_source_note(result)
    has_corrected = result.get("ventilation_ach_measured") is not None
    nominal_label = ("eACH_uv, steady-state CFD-fit (assumes nominal design ACH"
                      + (" - see measured-ACH row below for the corrected value)" if has_corrected else ")"))
    rows += [
        ("Reduction", f"{result['reduction_pct']:.1f}%{ach_note}"),
        (nominal_label, f"{result['eACH_uv_steady_state']:.4g} /hr{ach_note}"),
    ]
    if has_corrected:
        rows.append(("Effective ventilation ACH (well-mixed-equivalent, from Phase 1)",
                      f"{result['ventilation_ach_measured']:.4g} /hr{ach_note}"))
        rows.append(("eACH_uv, steady-state CFD-fit (measured ventilation ACH)",
                      f"{result['eACH_uv_steady_state_corrected']:.4g} /hr{ach_note}"))
    rows += _monitoring_summary_rows(result.get("monitoring"))
    return [html.Div([html.Span(k + ": ", className="text-muted"), html.Span(v)], className="mb-1")
            for k, v in rows] + _result_notes(result)


def _decay_summary(result):
    rows = []
    if result.get("fluence_mean") is not None:
        rows.append(("Average fluence rate", f"{result['fluence_mean']:.4g} µW/cm²"))
    rows += [
        ("Ventilation ACH (nominal)", f"{result['ventilation_ach']:.3g} /hr"),
        ("eACH_uv, well-mixed (idealized: Z x E_avg)", f"{result['eACH_uv_well_mixed']:.4g} /hr"),
        ("eACH_uv, CFD-fit (nominal ventilation ACH)", f"{result['eACH_uv_effective']:.4g} /hr"),
        ("Total ACH, effective", f"{result.get('total_ach_effective', 0):.3g} /hr"),
    ]
    if result.get("mixing_efficiency") is not None:
        rows.append(("Mixing efficiency", f"{result['mixing_efficiency'] * 100:.1f}%"))
    if result.get("ventilation_ach_measured") is not None:
        rows.append(("Ventilation ACH (measured, UV-off control)",
                      f"{result['ventilation_ach_measured']:.4g} /hr"))
        rows.append(("eACH_uv, CFD-fit (measured ventilation ACH)",
                      f"{result['eACH_uv_effective_corrected']:.4g} /hr"))
        rows.append(("Mixing efficiency (using measured ventilation ACH)",
                      f"{result['mixing_efficiency_corrected'] * 100:.1f}%"))
    rows += _monitoring_summary_rows(result.get("monitoring"))
    return [html.Div([html.Span(k + ": ", className="text-muted"), html.Span(v)], className="mb-1")
            for k, v in rows] + _result_notes(result)


analysis_tab = dbc.Row([
    dbc.Col([
        dbc.Button("Load results.json...", id="load-results-btn", color="primary",
                   size="sm", className="w-100 mb-2"),
        dbc.Button("Export report (.docx)...", id="export-report-btn", color="secondary",
                   outline=True, size="sm", className="w-100 mb-2"),
        dbc.Button("Open in ParaView", id="open-paraview-btn", color="secondary",
                   outline=True, size="sm", className="w-100 mb-2"),
        html.Div(id="analysis-status", className="small text-muted mb-3"),
        html.Div(id="analysis-summary", className="small"),
    ], width=4),
    dbc.Col([
        dcc.Graph(id="analysis-graph", style={"height": "80vh"}, figure=_empty_analysis_figure()),
    ], width=8),
], className="mt-3")

# Advanced/cross-project tunables (see app_settings.py) - grouped
# top-to-bottom by how likely a user is to actually touch them:
# convergence tolerances (already revisited once already) -> decay solver
# timing -> mesh/zone resolution (expert tier, bottom).
_adv_defaults = load_advanced_settings()
settings_modal = dbc.Modal(
    [
        dbc.ModalHeader(dbc.ModalTitle("Advanced Settings")),
        dbc.ModalBody(
            [
                html.Div("Convergence tolerances", className="small fw-bold text-uppercase mb-1"),
                html.Div(
                    "How strictly each solve decides “this has settled” — loosen to "
                    "save wall-clock time, tighten if a case runs out of iterations while still "
                    "visibly drifting.",
                    className="small text-muted mb-2",
                ),
                _settings_field(
                    "settings-flow-rel-tol", "Flow convergence tolerance",
                    "How much the room-average pressure is allowed to change between "
                    "convergence-check chunks before the flow field counts as settled. Real "
                    "turbulent rooms often oscillate slightly rather than truly converging — "
                    "this tolerance avoids burning iterations chasing that noise. Lower = stricter "
                    "and slower; higher = looser and faster.",
                    "%", _adv_defaults["flow-rel-tol"],
                ),
                _settings_field(
                    "settings-flow-max-iterations", "Flow convergence max iterations",
                    "Hard cap on total simpleFoam/pimpleFoam iterations spent trying to converge "
                    "the flow field before giving up (or accepting a bounded oscillation - see "
                    "the run log). Raise this if a case genuinely needs more iterations to settle; "
                    "lower it to fail fast instead of burning a long time on a case that won't "
                    "converge.",
                    "iterations", _adv_defaults["flow-max-iterations"],
                ),
                _settings_field(
                    "settings-plateau-rel-tol", "Steady-state plateau tolerance",
                    "The trailing-window coefficient of variation (CV) below which a steady-state "
                    "phase (Phase 1/Phase 2) counts as “plateaued” - same trailing window (fraction "
                    "of samples) as the reported T_ss itself, so the “plateaued” message and the "
                    "actual result are always checking the same thing. Lower = stricter (demands a "
                    "flatter tail before declaring convergence); higher = looser.",
                    "%", _adv_defaults["plateau-rel-tol"],
                ),
                html.Hr(className="my-2"),
                html.Div("Solver stability (under-relaxation)", className="small fw-bold text-uppercase mb-1"),
                html.Div(
                    "Each SIMPLE solver iteration only takes a fraction of the step toward its "
                    "newly-computed value, instead of fully accepting it — this damps oscillation "
                    "that would otherwise grow and diverge (an unrelaxed iterative solve is a lot "
                    "like a spring with no friction: every overshoot gets bigger, not smaller). "
                    "Lower = more damping, more resistant to a diverging/oscillating solve, but "
                    "slower to converge. Higher = faster, but more prone to instability on harder "
                    "cases (elongated openings, inlet/outlet close together, strong local source "
                    "terms).",
                    className="small text-muted mb-2",
                ),
                _settings_field(
                    "settings-momentum-relaxation", "Momentum/turbulence relaxation",
                    "Damping factor for velocity (U) and turbulence (k, omega) each solver "
                    "iteration. 0.7 is the standard, well-tested default for room-ventilation "
                    "flows — raising it can speed up convergence on easy cases, but is the first "
                    "thing to lower if a run's flow field oscillates instead of settling.",
                    "", _adv_defaults["momentum-relaxation"],
                ),
                _settings_field(
                    "settings-scalar-relaxation", "Contaminant (T) relaxation",
                    "Damping factor for the transported contaminant field (T) each solver "
                    "iteration, independent of momentum/turbulence above — a stiff or strong "
                    "source/sink term can destabilize T even when the flow field itself is "
                    "perfectly well-behaved. Lower this first if a steady-state run's T grows "
                    "or oscillates without bound instead of settling toward equilibrium.",
                    "", _adv_defaults["scalar-relaxation"],
                ),
                html.Hr(className="my-2"),
                html.Div("Steady-state early stopping (experimental)",
                          className="small fw-bold text-uppercase mb-1"),
                html.Div(
                    "Each steady-state phase (Phase 1/Phase 2) can stop before its full configured "
                    "iteration budget once its extrapolated true steady-state value (\"Extrapolated "
                    "T∞\" - see the report/Analysis tab) has stopped changing across several checks, "
                    "instead of always running to completion. Purely an early exit — the configured "
                    "iteration count remains a hard upper bound either way, so this can only save "
                    "time, never make a run longer or change its final answer once T∞ genuinely has "
                    "settled. Backtested against a real run: 1% tolerance barely saved anything, "
                    "2-3% saved a real ~35% of the run without looking fragile (same stop point "
                    "across that whole range) — start around 2% and loosen further only if it's "
                    "still too conservative for your cases.",
                    className="small text-muted mb-2",
                ),
                _settings_checkbox_field(
                    "settings-t-infinity-early-stop-enabled", "Enable T∞ early stopping",
                    "Off by default - this is a new, not-yet-widely-validated mechanism. Turn on "
                    "once you've compared a couple of early-stopped runs against full runs on your "
                    "own projects and trust it.",
                    _adv_defaults["t-infinity-early-stop-enabled"],
                ),
                _settings_field(
                    "settings-t-infinity-rel-tol", "T∞ stability tolerance",
                    "How much 3 consecutive T∞ estimates (500 iterations apart) may differ from "
                    "each other before the phase counts as settled and stops early. Only takes "
                    "effect when the checkbox above is on.",
                    "%", _adv_defaults["t-infinity-rel-tol"],
                ),
                html.Hr(className="my-2"),
                html.Div("Steady-state time-step retention", className="small fw-bold text-uppercase mb-1"),
                html.Div(
                    "By default, a steady-state run only keeps its initial (0/) and final field "
                    "state on disk once each phase finishes — every intermediate write_interval "
                    "snapshot along the way gets cleared, so ParaView can only show the start and "
                    "end, not an animated progression. Turn this on to keep every snapshot instead "
                    "(renamed to one continuous iteration count spanning Phase 1 then Phase 2, so "
                    "ParaView's time slider plays through the whole run). Uses more disk space per "
                    "run — off by default.",
                    className="small text-muted mb-2",
                ),
                _settings_checkbox_field(
                    "settings-keep-all-timesteps", "Keep all time steps for ParaView",
                    "Off by default to keep case directories small. Turn on before a run if you "
                    "want to review the transient build-up/decay in ParaView afterward.",
                    _adv_defaults["keep-all-timesteps"],
                ),
                html.Hr(className="my-2"),
                html.Div("Decay-mode solver timing", className="small fw-bold text-uppercase mb-1"),
                html.Div(
                    "Only affects decay-mode runs (steady-state uses its own iteration counts, "
                    "set on the Project Setup tab).",
                    className="small text-muted mb-2",
                ),
                _settings_field(
                    "settings-pimple-delta-t", "Decay solver time step",
                    "The physical time step (seconds) the transient UV-decay solver (pimpleFoam) "
                    "advances by. Smaller steps are more numerically stable but take longer to "
                    "reach the same simulated duration.",
                    "s", _adv_defaults["pimple-delta-t"],
                ),
                html.Hr(className="my-2"),
                html.Div("Mesh & zone resolution", className="small fw-bold text-uppercase mb-1"),
                html.Div(
                    "Expert / rarely-changed — these affect mesh size and solve cost "
                    "directly. Leave at defaults unless you have a specific reason to adjust them.",
                    className="small text-muted mb-2",
                ),
                _settings_field(
                    "settings-mesh-cell-size", "Mesh cell size",
                    "The uniform cell size (meters) used to build the room's mesh. A cell is one "
                    "small cube-shaped control volume; the mesh is the whole room filled "
                    "edge-to-edge with thousands of these cells, like LEGO bricks filling a box — "
                    "cells bundle up into the mesh, not the other way round. Smaller cells resolve "
                    "airflow detail more accurately, but cost grows fast: halving this value "
                    "roughly eightfolds the total cell count (all 3 dimensions shrink at once), "
                    "not doubles it.",
                    "m", _adv_defaults["mesh-cell-size"],
                ),
                _settings_field(
                    "settings-uv-zone-bins", "UV inactivation zone bins",
                    "Every cell already gets its own continuously-varying fluence/inactivation "
                    "rate from the lamp calculation — but OpenFOAM's sink terms attach to a "
                    "cellZone (a named group of cells sharing one fixed rate), not to individual "
                    "cells. Giving every cell its own truly unique rate would mean one cellZone "
                    "per cell — tens of thousands of entries, impractical to build or solve. "
                    "Binning sorts cells by rate into this many groups instead, each becoming one "
                    "cellZone with one representative rate.",
                    "bins", _adv_defaults["uv-zone-bins"],
                ),
                _settings_field(
                    "settings-source-zone-size", "Source zone size",
                    "The physical side length (meters) of the cube-shaped cellZone used to inject "
                    "the contaminant source in steady-state mode. Larger zones dilute the "
                    "injection over more cells; smaller zones concentrate it into fewer, "
                    "higher-rate cells.",
                    "m", _adv_defaults["source-zone-size"],
                ),
                html.Div(id="settings-status", className="small text-success mt-2"),
            ],
            style={"maxHeight": "64vh", "overflowY": "auto"},
        ),
        dbc.ModalFooter([
            dbc.Button("Reset to defaults", id="settings-reset-btn", color="link", size="sm",
                       className="me-auto text-muted"),
            dbc.Button("Cancel", id="settings-cancel-btn", color="secondary", outline=True, size="sm"),
            dbc.Button("Save", id="settings-save-btn", color="primary", size="sm"),
        ]),
    ],
    id="settings-modal", is_open=False, size="lg",
)

app.layout = dbc.Container([
    dcc.Store(id="fresh-room-load"),
    dcc.Store(id="results-data"),
    dcc.Store(id="results-case-dir"),
    dcc.Interval(id="run-poll", interval=2000, n_intervals=0, disabled=True),
    dcc.ConfirmDialog(id="overwrite-confirm"),
    dbc.Modal(
        [
            dbc.ModalHeader(dbc.ModalTitle(id="help-modal-title")),
            dbc.ModalBody(dcc.Markdown(id="help-modal-body", link_target="_blank")),
        ],
        id="help-modal", is_open=False, size="lg", scrollable=True,
    ),
    settings_modal,
    dbc.Row(
        dbc.Col(html.H4("GUV-CFD", className="mt-3 mb-1"), width="auto"),
    ),
    dbc.Row(dbc.Col(html.Div(
        "Combining GUV lighting calculation with Open Foam",
        className="text-muted small mb-1",
    ))),
    dbc.Row([
        dbc.Col(dbc.DropdownMenu(
            label="File", color="light", size="sm",
            children=[
                dbc.DropdownMenuItem("Open Project...", id="menu-open"),
                dbc.DropdownMenuItem("Save Project", id="menu-save"),
                dbc.DropdownMenuItem("Save Project As...", id="menu-save-as"),
            ],
        ), width="auto"),
        dbc.Col(dbc.Button("Settings", id="menu-settings", color="light", size="sm"), width="auto"),
        dbc.Col(dbc.DropdownMenu(
            label="Help", color="light", size="sm",
            children=[
                dbc.DropdownMenuItem("About", id="menu-help-about"),
                dbc.DropdownMenuItem("License", id="menu-help-license"),
                dbc.DropdownMenuItem("References", id="menu-help-references"),
                dbc.DropdownMenuItem("OpenFOAM Notes", id="menu-help-openfoam"),
            ],
        ), width="auto"),
        dbc.Col(html.Div([
            html.Span("Project file: ", className="text-muted"),
            html.Span("Untitled project", id="project-name-display",
                       className="text-muted fst-italic"),
        ]), width="auto", className="ms-3"),
    ], align="center", className="g-3 mt-2 mb-3"),
    dbc.Tabs([
        dbc.Tab(project_setup_tab, label="Project Setup", tab_id="project-setup"),
        dbc.Tab(processing_tab, label="Processing", tab_id="processing"),
        dbc.Tab(scenario_tab, label="Scenario Runs", tab_id="scenario-runs"),
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


# Which room dimension each opening's two position fields (named "-y-input"/
# "-z-input" for historical xMin/xMax-only reasons) actually bound against,
# now that an opening can be on any of the 6 walls - mirrors mesh_gen.
# _WALL_SPECS' in-plane-axis convention (e.g. floor/ceiling vary in x/y,
# not y/z). _register_position_field's own room-load reset still assumes
# the field's original dim (y/z) - fine for the default xMin/xMax walls;
# this callback keeps the slider bounds correct after the wall dropdown
# changes to something else.
_WALL_POSITION_DIMS = WALL_POSITION_DIMS


def _register_opening_wall_axes(prefix):
    # allow_duplicate=True: _register_position_field's own per-field
    # callback already owns {prefix}-y/z-slider/input's "max" (as part of
    # its "reset to room dimensions on fresh load" behavior) - this
    # callback is a second, independent writer to those same four outputs,
    # firing on a different trigger (the wall dropdown, not fresh-room-load).
    @app.callback(
        Output(f"{prefix}-y-slider", "max", allow_duplicate=True),
        Output(f"{prefix}-y-input", "max", allow_duplicate=True),
        Output(f"{prefix}-z-slider", "max", allow_duplicate=True),
        Output(f"{prefix}-z-input", "max", allow_duplicate=True),
        Input(f"{prefix}-wall", "value"),
        prevent_initial_call=True,
    )
    def _update_bounds(wall):
        room = _loaded["room"]
        if room is None or wall not in _WALL_POSITION_DIMS:
            return dash.no_update, dash.no_update, dash.no_update, dash.no_update
        dim1, dim2 = _WALL_POSITION_DIMS[wall]
        max1, max2 = round(getattr(room, dim1), 3), round(getattr(room, dim2), 3)
        return max1, max1, max2, max2

    _update_bounds.__name__ = f"_wall_axes_{prefix.replace('-', '_')}"


for _opening_prefix in ("inlet", "outlet", "inlet2", "outlet2"):
    _register_opening_wall_axes(_opening_prefix)


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
    Output("inlet2-controls", "style"),
    Input("inlet2-enable", "value"),
)
def _toggle_inlet2_controls(enabled):
    return {"display": "block"} if enabled else {"display": "none", "opacity": "0.4"}


@app.callback(
    Output("outlet2-controls", "style"),
    Input("outlet2-enable", "value"),
)
def _toggle_outlet2_controls(enabled):
    return {"display": "block"} if enabled else {"display": "none", "opacity": "0.4"}


@app.callback(
    Output("monitoring-controls", "style"),
    Input("monitoring-enable", "value"),
)
def _toggle_monitoring_controls(enabled):
    return {"display": "block"} if enabled else {"display": "none", "opacity": "0.4"}


def _register_monitor_point_toggle(i):
    @app.callback(
        Output(f"monitor{i}-controls", "style"),
        Input(f"monitor{i}-enable", "value"),
    )
    def _toggle(enabled):
        return {"display": "block"} if enabled else {"display": "none", "opacity": "0.4"}

    _toggle.__name__ = f"_toggle_monitor{i}_controls"


for _i in MONITOR_POINT_IDS:
    _register_monitor_point_toggle(_i)


@app.callback(
    Output("pimple-end-time", "value"),
    Input("suggest-duration-btn", "n_clicks"),
    Input("fresh-room-load", "data"),
    State("ach", "value"),
    State("z-value", "value"),
    prevent_initial_call=True,
)
def _suggest_duration(n_clicks, _fresh_load, ach, z_value):
    room = _loaded["room"]
    if room is None or ach is None or z_value is None:
        return dash.no_update
    eACH = _estimate_well_mixed_eACH(room, z_value)
    return _settling_iterations(ach + eACH, target_fraction=0.99, min_iterations=10, max_iterations=7200)


@app.callback(
    Output("phase1-iterations", "value"),
    Output("phase2-iterations", "value"),
    Input("suggest-phases-btn", "n_clicks"),
    Input("fresh-room-load", "data"),
    State("ach", "value"),
    State("z-value", "value"),
    prevent_initial_call=True,
)
def _suggest_phases(n_clicks, _fresh_load, ach, z_value):
    room = _loaded["room"]
    if room is None or ach is None or z_value is None:
        return dash.no_update, dash.no_update
    eACH = _estimate_well_mixed_eACH(room, z_value)
    return (_settling_iterations(ach, target_fraction=0.995),
            _settling_iterations(ach + eACH, target_fraction=0.995))


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
    Output("results-case-dir", "data", allow_duplicate=True),
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
        return dash.no_update, dash.no_update, dash.no_update
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:
        return dash.no_update, f"Failed to load: {e}", dash.no_update
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    case_dir = path.replace("\\", "/").rsplit("/", 1)[0]
    return data, f"Loaded {name}", case_dir


def _default_report_name(results_case_dir):
    """<.guv project name>_report.docx, using the project that *this run*
    was actually built from (run_settings.json's own guv_path) - not
    _loaded["settings_path"] (the Setup tab's currently-open project), which
    can be a different, unrelated, or stale project from whatever run the
    Analysis tab happens to be showing right now. Falls back to the
    Setup tab's project, then the case directory name, if run_settings.json
    is missing or predates the guv_path field.
    """
    run_settings_path = Path(results_case_dir) / "run_settings.json"
    if run_settings_path.exists():
        try:
            with open(run_settings_path) as f:
                guv_path = json.load(f).get("guv_path")
            if guv_path:
                return f"{Path(guv_path).stem}_report.docx"
        except (json.JSONDecodeError, OSError):
            pass
    settings_path = _loaded.get("settings_path")
    stem = Path(settings_path).stem if settings_path else Path(results_case_dir).name
    return f"{stem}_report.docx"


@app.callback(
    Output("analysis-status", "children", allow_duplicate=True),
    Input("export-report-btn", "n_clicks"),
    State("results-case-dir", "data"),
    prevent_initial_call=True,
)
def _export_report(n_clicks, results_case_dir):
    if not results_case_dir:
        return "Load a results.json first (or finish a run) before exporting a report."
    out_path = _native_save_file(
        "Export simulation report",
        ".docx",
        [("Word document", "*.docx"), ("All files", "*.*")],
        initialfile=_default_report_name(results_case_dir),
    )
    if not out_path:
        return dash.no_update
    try:
        generate_report_docx(results_case_dir, out_path)
    except Exception as e:
        return f"Failed to export report: {e}"
    name = out_path.replace("\\", "/").rsplit("/", 1)[-1]
    return f"Report saved to {name}"


@app.callback(
    Output("analysis-status", "children", allow_duplicate=True),
    Input("open-paraview-btn", "n_clicks"),
    State("results-case-dir", "data"),
    prevent_initial_call=True,
)
def _open_paraview(n_clicks, results_case_dir):
    if not results_case_dir:
        return "Load a results.json first (or finish a run) before opening ParaView."
    settings_path = Path(results_case_dir) / "run_settings.json"
    if not settings_path.exists():
        return (f"{results_case_dir}/run_settings.json not found - rerun a full "
                f"simulation here to enable the ParaView preset.")
    with open(settings_path) as f:
        settings = json.load(f)
    try:
        points = read_cell_centers(results_case_dir, "0")
        mesh_bounds = (points[:, 0].min(), points[:, 0].max(),
                       points[:, 1].min(), points[:, 1].max(),
                       points[:, 2].min(), points[:, 2].max())
        source_center = settings.get("source_center")
        if source_center and any(v is None for v in source_center):
            source_center = None  # incomplete/old record - skip the 3rd view rather than crash
        launch_paraview(results_case_dir, mesh_bounds, source_center=source_center)
    except Exception as e:
        return f"Failed to open ParaView: {e}"
    msg = "Opened ParaView (log-scale volume T + room-seeded streamlines colored by U"
    if source_center:
        msg += " + source-seeded streamlines colored by T"
    return msg + ")."


@app.callback(
    Output("analysis-graph", "figure"),
    Output("analysis-summary", "children"),
    Input("results-data", "data"),
)
def _render_analysis(data):
    if not data:
        return _empty_analysis_figure(), []
    if "phase1" in data:
        return steady_state_figure(data), _steady_state_summary(data)
    return decay_figure(data), _decay_summary(data)


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


_HELP_CONTENT = {
    "menu-help-about": ("About", help_content.ABOUT),
    "menu-help-license": ("License", help_content.LICENSE_SUMMARY),
    "menu-help-references": ("References", help_content.REFERENCES),
    "menu-help-openfoam": ("OpenFOAM Notes", help_content.OPENFOAM_NOTES),
}


@app.callback(
    Output("help-modal", "is_open"),
    Output("help-modal-title", "children"),
    Output("help-modal-body", "children"),
    Input("menu-help-about", "n_clicks"),
    Input("menu-help-license", "n_clicks"),
    Input("menu-help-references", "n_clicks"),
    Input("menu-help-openfoam", "n_clicks"),
    prevent_initial_call=True,
)
def _open_help_modal(*_clicks):
    title, body = _HELP_CONTENT[dash.ctx.triggered_id]
    return True, title, body


_SETTINGS_FIELD_IDS = [
    "settings-flow-rel-tol", "settings-flow-max-iterations", "settings-plateau-rel-tol",
    "settings-momentum-relaxation", "settings-scalar-relaxation",
    "settings-t-infinity-early-stop-enabled", "settings-t-infinity-rel-tol",
    "settings-keep-all-timesteps",
    "settings-pimple-delta-t", "settings-mesh-cell-size",
    "settings-uv-zone-bins", "settings-source-zone-size",
]
# Same order as _SETTINGS_FIELD_IDS - maps each GUI field to its
# app_settings.py storage key (see ADVANCED_SETTINGS_DEFAULTS).
_SETTINGS_FIELD_KEYS = [
    "flow-rel-tol", "flow-max-iterations", "plateau-rel-tol",
    "momentum-relaxation", "scalar-relaxation",
    "t-infinity-early-stop-enabled", "t-infinity-rel-tol",
    "keep-all-timesteps",
    "pimple-delta-t", "mesh-cell-size", "uv-zone-bins", "source-zone-size",
]


@app.callback(
    Output("settings-modal", "is_open"),
    Input("menu-settings", "n_clicks"),
    Input("settings-cancel-btn", "n_clicks"),
    Input("settings-save-btn", "n_clicks"),
    State("settings-modal", "is_open"),
    prevent_initial_call=True,
)
def _toggle_settings_modal(_open, _cancel, _save, is_open):
    return not is_open


@app.callback(
    [Output(fid, "value") for fid in _SETTINGS_FIELD_IDS],
    Input("menu-settings", "n_clicks"),
    prevent_initial_call=True,
)
def _populate_settings_modal(_n):
    # Fresh read on every open (not just at app startup) - so a value
    # changed by hand-editing advanced_settings.json, or by another
    # instance of the app, is always what the modal shows.
    saved = load_advanced_settings()
    return [saved[k] for k in _SETTINGS_FIELD_KEYS]


@app.callback(
    [Output(fid, "value", allow_duplicate=True) for fid in _SETTINGS_FIELD_IDS],
    Input("settings-reset-btn", "n_clicks"),
    prevent_initial_call=True,
)
def _reset_settings_modal(_n):
    return [ADVANCED_SETTINGS_DEFAULTS[k] for k in _SETTINGS_FIELD_KEYS]


@app.callback(
    Output("settings-status", "children"),
    Input("settings-save-btn", "n_clicks"),
    [State(fid, "value") for fid in _SETTINGS_FIELD_IDS],
    prevent_initial_call=True,
)
def _save_settings(_n, *values):
    settings = dict(zip(_SETTINGS_FIELD_KEYS, values))
    save_advanced_settings(settings)
    return "Saved."


# Fallback values for fields that predate a project file's save - loading
# a .guvcfd saved before the 2nd-inlet/2nd-outlet feature existed leaves
# these keys missing from the JSON, and settings.get(fid) alone would push
# a bare None into e.g. the wall dropdowns, crashing anything that looks
# the wall up (_center_frac_for_wall etc.) the moment the field is enabled
# - even though "enabled" itself defaults safely to None/falsy. Values
# here match the layout's own component defaults (_opening_controls's
# "ceiling"/"floor", POSITION_FIELDS' inlet2/outlet2 defaults).
_NEW_FIELD_DEFAULTS = {
    "inlet2-enable": False, "inlet2-wall": "ceiling",
    "inlet2-y-input": 2.0, "inlet2-z-input": 1.5,
    "inlet2-size-w": 0.3, "inlet2-size-h": 0.3,
    "outlet2-enable": False, "outlet2-wall": "floor",
    "outlet2-y-input": 2.0, "outlet2-z-input": 1.5,
    "outlet2-size-w": 0.3, "outlet2-size-h": 0.3,
    "t-ss-window-frac": 0.15,
    "inlet-diffuser-type": "direct", "inlet2-diffuser-type": "direct",
}


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

    field_values = [settings.get(fid, _NEW_FIELD_DEFAULTS.get(fid)) for fid in SETTINGS_FIELDS]
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


def _scenario_sweep_thread(guv_path, settings_path, project_dir, room, settings, adv, z_values, ach_values):
    def on_combo_done(z, ach, status, detail):
        _scenario_state["results"][(z, ach)] = {"status": status, "detail": detail}

    try:
        scenario_runs.run_sweep(
            guv_path, settings_path, project_dir, room, settings, adv,
            z_values, ach_values, log_fn=_scenario_log, should_stop=_scenario_should_stop,
            on_combo_done=on_combo_done,
        )
        _scenario_state["status"] = "done"
    except StoppedByUser as e:
        _scenario_log(f"Stopped: {e}")
        _scenario_state["status"] = "stopped"
    except Exception as e:
        _scenario_log(f"ERROR: {e}")
        _scenario_state["status"] = "error"


def _launch_scenario_sweep(guv_path, settings_path, project_dir, room, settings, adv, z_values, ach_values):
    combos = scenario_runs.sweep_combinations(z_values, ach_values)
    _scenario_state.update(status="running", log=[], combos=combos, results={},
                            start_time=time.time(), stop_requested=False)
    thread = threading.Thread(
        target=_scenario_sweep_thread,
        args=(guv_path, settings_path, project_dir, room, settings, adv, z_values, ach_values),
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

    missing = _validate_settings(settings)
    if missing:
        return (False, False, True,
                "Missing required value(s) - fill these in before running: "
                + ", ".join(missing) + ".", dash.no_update, False, dash.no_update)

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
    clear_stale_run_output(_pending_run["case_dir"])
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


@app.callback(
    Output("scenario-combo-count", "children"),
    Input("scenario-z-values", "value"),
    Input("scenario-ach-values", "value"),
)
def _update_scenario_combo_count(z_text, ach_text):
    try:
        z_values = _parse_number_list(z_text)
        ach_values = _parse_number_list(ach_text)
    except ValueError as e:
        return f"Can't parse: {e}"
    if not z_values or not ach_values:
        return "Enter at least one Z value and one ACH value."
    n = len(scenario_runs.sweep_combinations(z_values, ach_values))
    return f"{n} combination{'s' if n != 1 else ''} ({len(z_values)} Z x {len(ach_values)} ACH)."


@app.callback(
    Output("scenario-run-btn", "disabled"),
    Output("scenario-stop-btn", "disabled"),
    Output("scenario-poll", "disabled"),
    Output("scenario-validation-msg", "children"),
    Output("main-tabs", "active_tab", allow_duplicate=True),
    Input("scenario-run-btn", "n_clicks"),
    State("scenario-z-values", "value"),
    State("scenario-ach-values", "value"),
    [State(fid, "value") for fid in SETTINGS_FIELDS],
    prevent_initial_call=True,
)
def _start_scenario_sweep(n_clicks, z_text, ach_text, *values):
    if _scenario_state["status"] == "running":
        return True, False, False, dash.no_update, dash.no_update

    room = _loaded["room"]
    guv_path = _loaded["path"]
    if room is None or guv_path is None:
        return (False, True, True, "No .guv project loaded - use File > Open Project or "
                "Load .guv file first.", dash.no_update)

    settings = dict(zip(SETTINGS_FIELDS, values))
    if settings.get("sim-type") != "steady_state":
        return (False, True, True, "Scenario Runs only supports steady-state projects "
                "(set Simulation type to Steady State on the Project Setup tab).", dash.no_update)
    if not settings.get("case-dir"):
        return False, True, True, "Set an OpenFOAM project directory first.", dash.no_update

    try:
        z_values = _parse_number_list(z_text)
        ach_values = _parse_number_list(ach_text)
    except ValueError as e:
        return False, True, True, f"Can't parse Z/ACH list: {e}", dash.no_update
    if not z_values or not ach_values:
        return False, True, True, "Enter at least one Z value and one ACH value.", dash.no_update

    missing = _validate_settings(settings)
    if missing:
        return (False, True, True,
                "Missing required value(s) - fill these in on Project Setup before running: "
                + ", ".join(missing) + ".", dash.no_update)

    adv = load_advanced_settings()
    _launch_scenario_sweep(guv_path, _loaded.get("settings_path"), settings["case-dir"],
                            room, settings, adv, z_values, ach_values)
    return True, False, False, "", "scenario-runs"


@app.callback(
    Output("scenario-log", "children", allow_duplicate=True),
    Input("scenario-stop-btn", "n_clicks"),
    prevent_initial_call=True,
)
def _stop_scenario_sweep(n_clicks):
    if _scenario_state["status"] == "running":
        _scenario_state["stop_requested"] = True
        _scenario_log("Stop requested - the sweep will stop before its next combination...")
    return (dash.no_update,)


def _scenario_progress_table():
    combos = _scenario_state["combos"]
    if not combos:
        return html.Div("No sweep run yet.", className="small text-muted")
    results = _scenario_state["results"]
    header = html.Tr([html.Th("Z"), html.Th("ACH"), html.Th("Status"), html.Th("Reduction"), html.Th("eACH_uv")])
    rows = [header]
    for z, ach in combos:
        entry = results.get((z, ach))
        if entry is None:
            status, reduction, eACH = "pending", "", ""
        elif entry["status"] == "done":
            detail = entry["detail"]
            status = "done"
            reduction = f"{detail['reduction_pct']:.1f}%"
            eACH = f"{detail['eACH_uv_steady_state']:.4g} /hr"
        else:
            status, reduction, eACH = f"error: {entry['detail']}", "", ""
        rows.append(html.Tr([html.Td(z), html.Td(ach), html.Td(status), html.Td(reduction), html.Td(eACH)]))
    return dbc.Table(rows, bordered=False, hover=True, size="sm", className="small")


@app.callback(
    Output("scenario-log", "children"),
    Output("scenario-status-text", "children"),
    Output("scenario-progress-table", "children"),
    Output("scenario-poll", "disabled", allow_duplicate=True),
    Output("scenario-run-btn", "disabled", allow_duplicate=True),
    Output("scenario-stop-btn", "disabled", allow_duplicate=True),
    Input("scenario-poll", "n_intervals"),
    prevent_initial_call=True,
)
def _poll_scenario(n_intervals):
    status = _scenario_state["status"]
    log_text = "\n".join(_scenario_state["log"][-300:])
    n_done = sum(1 for r in _scenario_state["results"].values() if r["status"] == "done")
    n_error = sum(1 for r in _scenario_state["results"].values() if r["status"] == "error")
    n_total = len(_scenario_state["combos"])
    status_text = {
        "running": f"Running... ({n_done + n_error}/{n_total} combinations done)",
        "done": f"Finished. {n_done}/{n_total} succeeded, {n_error} failed.",
        "error": "Failed - see log below.",
        "stopped": f"Stopped. {n_done}/{n_total} succeeded, {n_error} failed.",
    }.get(status, "")
    still_running = status == "running"
    return (log_text, status_text, _scenario_progress_table(),
            not still_running, still_running, not still_running)


def _render_checklist():
    icons = {"pending": "☐", "running": "▶", "done": "☑"}
    colors = {"pending": "text-muted", "running": "text-primary fw-semibold", "done": "text-success"}
    steps = _run_state.get("steps") or DECAY_STEPS
    status = _run_state.get("step_status", {})
    return [
        html.Li(f"{icons[status.get(s, 'pending')]} {s}", className=colors[status.get(s, "pending")])
        for s in steps
    ]


def _format_mmss(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _solver_progress_text():
    """'Simulation time step X of Y (pct%)' line for the Processing tab:
    current/target within the phase currently running (a flow-convergence
    chunk, a steady-state phase, or the pimpleFoam decay run - see
    _PHASE_TARGET_PATTERNS). The ETA is a separate line - see
    _solver_eta_text() - so the Processing tab can put them on their own
    lines instead of cramming both into one.
    """
    cur = _run_state.get("current_time")
    if not cur:
        return ""
    try:
        cur_val = float(cur)
    except (TypeError, ValueError):
        return f"Simulation time step {cur}"

    target = _run_state.get("target_time")
    phase_start = _run_state.get("phase_start_time")
    if not target or not phase_start:
        return f"Simulation time step {cur_val:.4g}"

    pct = min(100, round(100 * cur_val / target))
    return f"Simulation time step {cur_val:.4g} of {target:.4g} ({pct}%)"


def _solver_eta_text():
    """'Expected finish of this step in M:SS' line, extrapolated from how
    fast Time has advanced since the current phase started (not the whole
    run's elapsed time - an earlier phase's pace would otherwise skew the
    estimate). "" if there isn't enough information yet.
    """
    cur = _run_state.get("current_time")
    target = _run_state.get("target_time")
    phase_start = _run_state.get("phase_start_time")
    if not cur or not target or not phase_start:
        return ""
    try:
        cur_val = float(cur)
    except (TypeError, ValueError):
        return ""
    elapsed = time.time() - phase_start
    if cur_val <= 0 or elapsed <= 0:
        return ""
    rate = cur_val / elapsed
    if rate <= 0:
        return ""
    return f"Expected finish of this step in {_format_mmss((target - cur_val) / rate)}"


@app.callback(
    Output("run-log", "children"),
    Output("run-status-text", "children"),
    Output("run-btn", "disabled", allow_duplicate=True),
    Output("continue-btn", "disabled", allow_duplicate=True),
    Output("run-poll", "disabled"),
    Output("stop-btn", "disabled"),
    Output("run-checklist", "children"),
    Output("run-elapsed", "children"),
    Output("run-current-time", "children"),
    Output("results-data", "data", allow_duplicate=True),
    Output("results-case-dir", "data", allow_duplicate=True),
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

    start = _run_state.get("start_time")
    elapsed = f"Elapsed: {_format_mmss(time.time() - start)}" if start else ""
    progress_line = _solver_progress_text()
    eta_line = _solver_eta_text()
    cur_time_text = [progress_line, html.Br(), eta_line] if progress_line and eta_line else progress_line

    # Auto-load this run's own results once it finishes, so the Analysis
    # tab has something to show without a separate manual step - polling
    # stops right after this (run-poll.disabled becomes True), so this
    # only fires once, exactly when status first becomes "done".
    results_data = dash.no_update
    results_case_dir = dash.no_update
    if status == "done" and _run_state.get("case_dir"):
        try:
            with open(f"{_run_state['case_dir']}/results.json") as f:
                results_data = json.load(f)
            results_case_dir = _run_state["case_dir"]
        except Exception:
            results_data = dash.no_update

    return (log_text, status_text, still_running, still_running, not still_running, not still_running,
            _render_checklist(), elapsed, cur_time_text, results_data, results_case_dir)


@app.callback(
    Output("preview-graph", "figure"),
    Input("project-status", "children"),
    Input("inlet-show", "value"), Input("inlet-wall", "value"),
    Input("inlet-y-input", "value"), Input("inlet-z-input", "value"),
    Input("inlet-size-w", "value"), Input("inlet-size-h", "value"),
    Input("outlet-show", "value"), Input("outlet-wall", "value"),
    Input("outlet-y-input", "value"), Input("outlet-z-input", "value"),
    Input("outlet-size-w", "value"), Input("outlet-size-h", "value"),
    Input("inlet2-enable", "value"), Input("inlet2-wall", "value"),
    Input("inlet2-y-input", "value"), Input("inlet2-z-input", "value"),
    Input("inlet2-size-w", "value"), Input("inlet2-size-h", "value"),
    Input("outlet2-enable", "value"), Input("outlet2-wall", "value"),
    Input("outlet2-y-input", "value"), Input("outlet2-z-input", "value"),
    Input("outlet2-size-w", "value"), Input("outlet2-size-h", "value"),
    Input("fan-enable", "value"), Input("fan-speed", "value"), Input("fan-direction", "value"),
    Input("fan-radius", "value"), Input("fan-thickness", "value"),
    Input("fan-x-input", "value"), Input("fan-y-input", "value"), Input("fan-z-input", "value"),
    Input("sim-type", "value"),
    Input("inject-x-input", "value"), Input("inject-y-input", "value"), Input("inject-z-input", "value"),
    Input("monitoring-enable", "value"),
    *[Input(f"monitor{i}-{suffix}", "value")
      for i in MONITOR_POINT_IDS
      for suffix in ("enable", "name", "x-input", "y-input", "z-input", "cells")],
)
def _update_preview(_status, inlet_show, inlet_wall, inlet_y, inlet_z, inlet_w, inlet_h,
                     outlet_show, outlet_wall, outlet_y, outlet_z, outlet_w, outlet_h,
                     inlet2_enable, inlet2_wall, inlet2_y, inlet2_z, inlet2_w, inlet2_h,
                     outlet2_enable, outlet2_wall, outlet2_y, outlet2_z, outlet2_w, outlet2_h,
                     fan_enable, fan_speed, fan_direction, fan_radius, fan_thickness,
                     fan_x, fan_y, fan_z, sim_type, inject_x, inject_y, inject_z,
                     monitoring_enable, *monitor_values):
    room = _loaded["room"]
    if room is None:
        return _empty_preview_figure()

    inlet_center = _center_frac_for_wall(inlet_wall, inlet_y, inlet_z, room)
    outlet_center = _center_frac_for_wall(outlet_wall, outlet_y, outlet_z, room)

    opening2_kwargs = {}
    if inlet2_enable:
        opening2_kwargs.update(
            inlet2_wall=inlet2_wall, inlet2_center=_center_frac_for_wall(inlet2_wall, inlet2_y, inlet2_z, room),
            inlet2_size=(inlet2_w, inlet2_h),
        )
    if outlet2_enable:
        opening2_kwargs.update(
            outlet2_wall=outlet2_wall, outlet2_center=_center_frac_for_wall(outlet2_wall, outlet2_y, outlet2_z, room),
            outlet2_size=(outlet2_w, outlet2_h),
        )

    fan_kwargs = {}
    if fan_enable:
        direction = (0, 0, -1) if fan_direction == "down" else (0, 0, 1)
        fan_kwargs = dict(
            fan_speed=fan_speed, fan_disk_radius=fan_radius, fan_disk_thickness=fan_thickness,
            fan_center=(fan_x, fan_y, fan_z), fan_direction=direction,
        )

    injection_center = (inject_x, inject_y, inject_z) if sim_type == "steady_state" else None

    monitor_field_ids = [f"monitor{i}-{suffix}" for i in MONITOR_POINT_IDS
                          for suffix in ("enable", "name", "x-input", "y-input", "z-input", "cells")]
    monitoring_settings = dict(zip(monitor_field_ids, monitor_values))
    monitoring_settings["monitoring-enable"] = monitoring_enable
    monitoring_points = _gather_monitoring_points(monitoring_settings)

    fig = plot_case(
        room,
        inlet_wall=inlet_wall, inlet_center=inlet_center, inlet_size=(inlet_w, inlet_h),
        outlet_wall=outlet_wall, outlet_center=outlet_center, outlet_size=(outlet_w, outlet_h),
        injection_center=injection_center,
        monitoring_points=monitoring_points,
        title="", **fan_kwargs, **opening2_kwargs,
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
