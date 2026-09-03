"""GUV-CFD GUI: load a .guv project, configure inlet/outlet/fan and the
scenario type, preview the 3D case setup live, and (eventually) run the
pipeline. Local single-user tool - run `python -m guvcfd.app` and open
the printed localhost URL.
"""
import csv
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

from .app_settings import (
    ADVANCED_SETTINGS_DEFAULTS, load_advanced_settings, save_advanced_settings,
    merge_project_openfoam_settings, capture_openfoam_settings, PROJECT_OPENFOAM_SETTINGS_KEYS,
)
from .case_io import (
    clear_stale_run_output, read_cell_centers, read_latest_time_field, snapshot_openfoam_settings,
)
from .decay_analysis import (write_results_summary, mechanical_mixing_efficiency_pct,
                              spatial_coefficient_of_variation, read_vol_average_dat,
                              run_decay_to_target, read_decay_curve, migrate_result_keys)
from .monitoring import splice_live_vol_average_if_needed
from .contaminant_source import (breathing_inlet_direction, breathing_inlet_velocity,
                                  breathing_inlet_velocity_constraint, resolve_source_size)
from .fan import fan_fvoptions_entry, fan_entry_from_settings
from .fluence import compute_fluence_at_points, compute_inactivation_rate, compute_well_mixed_eACH
from . import help_content
from .version import APP_VERSION
from .initial_fields import compute_inlet_velocities
from .monitoring_points import compute_monitoring_results, mixing_uniformity_note, point_reduction_basis
from .paraview_launch import launch_paraview
from .report import (
    generate_report_docx, T_FIELD_NOTE, _effective_ach_note, _phase_ss_rows, _ach_source_note,
    combo_summary_metrics,
)
from .result_figures import steady_state_figure, decay_figure
from .run_pipeline import (
    setup_case, resume_case_setup, case_awaiting_flow_decision, FlowConvergenceUndecided,
    check_settings_grid_alignment, walk_opening_alignment_conflicts,
)
from . import scenario_runs
from .mesh_gen import opening_actual_area
from .project_status import clear_ach_bases, load_project_status
from .splice import set_control_dict_start_from, set_control_dict_time, set_relaxation_factors
from .steady_state_pipeline import (
    run_steady_state_scenario, _read_phase1_checkpoint, _clear_phase1_checkpoint, Phase1ExtrapolationUndecided,
    resolve_phase_delta_ts, merge_project_deltat_settings, REFERENCE_TARGET_T_SS,
)
from .tclamp_decay import ensure_tclamp_decay_compiled, splice_tclamp_decay_if_needed
from .ventilation_control import prepare_ventilation_only_control, finish_ventilation_only_control
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
    "sim-type", "mech-ach-only", "pimple-end-time", "pimple-write-interval",
    "inject-x-input", "inject-y-input", "inject-z-input", "source-zone-cells",
    "breathing-velocity", "breathing-dir-x", "breathing-dir-y", "breathing-dir-z",
    "phase1-iterations", "phase2-iterations", "t-ss-window-frac",
    "deltat-scaling-enabled", "deltat-effective-fraction", "deltat-target-fraction",
    "scenario-z-values", "scenario-ach-values",
    "monitoring-enable",
    "monitor1-enable", "monitor1-name", "monitor1-x-input", "monitor1-y-input",
    "monitor1-z-input", "monitor1-cells",
    "monitor2-enable", "monitor2-name", "monitor2-x-input", "monitor2-y-input",
    "monitor2-z-input", "monitor2-cells",
    "monitor3-enable", "monitor3-name", "monitor3-x-input", "monitor3-y-input",
    "monitor3-z-input", "monitor3-cells",
]

MONITOR_POINT_IDS = [1, 2, 3]


def _collect_settings(values):
    """dict(zip(SETTINGS_FIELDS, values)), enriched with any
    PROJECT_OPENFOAM_SETTINGS_KEYS captured/hand-edited into the
    currently-loaded .guvcfd file on disk but not present here. Those keys
    have no Dash UI component (the "JSON-embedded block, no new UI yet"
    design decision) - without this, dict(zip(SETTINGS_FIELDS, values))
    alone would never see a hand-edited override (e.g. a copied .guvcfd
    with max-co changed to try a variant), and worse, the next save would
    silently overwrite it back to the global default via
    capture_openfoam_settings (which only backfills a key that's
    genuinely missing - it can't tell "missing because never captured"
    from "missing because this dict just never had it wired in").
    Best-effort: a missing/unreadable file is a no-op enrichment, not a
    hard requirement.
    """
    settings = dict(zip(SETTINGS_FIELDS, values))
    settings_path = _loaded.get("settings_path")
    if settings_path:
        try:
            with open(settings_path) as f:
                raw = json.load(f)
            for key in PROJECT_OPENFOAM_SETTINGS_KEYS:
                if key in raw:
                    settings[key] = raw[key]
        except (OSError, json.JSONDecodeError):
            pass
    return settings

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
    "current_time": None, "start_time": None, "stop_requested": False, "pause_requested": False,
    # Per-stream "latest Time = N" status for a run with more than one
    # concurrently-solving pimpleFoam process (decay mode's UV-on + UV-off
    # control pair - see _run_decay_pair) - overwritten in place, not
    # appended, so "Running now" can show both streams' progress without
    # flooding the scrolling log the way appending every "Time = N" line
    # used to (see _run_status_update/_throttled_solver_callback). Empty
    # for every other run type (steady-state, flow convergence, Continue),
    # which only ever have one solver running at a time and already show
    # their own progress via current_time/_solver_progress_text() instead.
    "live_status": {},
    # Set only when status == "awaiting_decision" (see FlowConvergenceUndecided/
    # _run_pipeline_thread) - everything the Processing tab's decision panel
    # needs to display the diagnostic and everything a Continue/Accept button
    # needs to resume (see _resume_pipeline_thread), without re-deriving
    # anything from the (possibly since-changed) GUI form fields.
    "decision": None,
    # Set only when status == "awaiting_phase2_resume" (see
    # case_awaiting_phase2_resume/_start_run) - everything the Processing
    # tab's resume panel needs to display and everything a Resume click
    # needs to finish the run (see _resume_phase2_thread), without
    # re-deriving anything from the (possibly since-changed) GUI form fields.
    "phase2_decision": None,
}

# Scenario Runs (Z x ACH sweep) state - deliberately its own dict rather
# than reusing _run_state, so a sweep and a normal single Run never cross
# wires (e.g. the Processing tab's Stop button must never abort a sweep,
# and vice versa). "results" is keyed by (z, ach) -> {"status": "done"/
# "error", "detail": trimmed result dict or an error message}.
_scenario_state = {
    "status": "idle", "log": [], "combos": [], "results": {},
    "start_time": None, "stop_requested": False, "pause_requested": False, "live_status": {},
    # One _new_progress_entry() per ACH-group/combo key (e.g. "ACH=6",
    # "Z=6/ACH=6") - see _update_progress_from_log_line/_status_line and
    # _combo_eta_text. Keyed the same way live_status/status_fn's own keys
    # are, MINUS the trailing "/Phase1"/"/Phase2"/"/UV-on"/"/control" phase
    # suffix (see _scenario_status_update) - one entry per group/combo, not
    # per phase, since a combo only ever has one phase active at a time.
    "progress": {},
}

# Matches _prefixed_log_fn's own "[prefix] message" wrapping (see
# scenario_runs.py) - lets _scenario_log recover which ACH-group/combo a
# narration line belongs to, the same way _scenario_status_update's own
# key already does for solver status lines, so both can update the SAME
# per-key progress entry (see _scenario_state["progress"]).
_SCENARIO_LOG_PREFIX_RE = re.compile(r"^\[([^\]]+)\] (.*)$")


def _scenario_log(msg):
    msg = str(msg)
    log = _scenario_state["log"]
    log.append(msg)
    if len(log) > _MAX_LOG_LINES:
        del log[: len(log) - _MAX_LOG_LINES]

    m = _SCENARIO_LOG_PREFIX_RE.match(msg)
    if m:
        prefix, rest = m.group(1), m.group(2)
        progress = _scenario_state["progress"].setdefault(prefix, _new_progress_entry())
        _update_progress_from_log_line(progress, rest)


def _scenario_status_update(key, msg):
    """Per-stream "latest Time = N" status, overwritten in place rather
    than appended - see scenario_runs._throttled_solver_callback's
    status_fn. Several concurrent ACH/Z combinations each printing their
    own Time=N line every step would otherwise flood _scenario_log the
    same way the raw per-iteration residual dump already doesn't. msg=None
    removes the entry (that stream's solve just finished).

    key is always log_prefix + "/" + a phase suffix (e.g. "ACH=6/Phase1",
    "Z=6/ACH=6/UV-on", "ACH=6/control" - see steady_state_pipeline.py's
    status_key1/status_key2 and scenario_runs.py's own status_key
    construction, all of which follow this convention deliberately so this
    rsplit recovers the SAME prefix _scenario_log's own _SCENARIO_LOG_
    PREFIX_RE already keys progress entries by).
    """
    live = _scenario_state["live_status"]
    prefix = key.rsplit("/", 1)[0]
    if msg is None:
        live.pop(key, None)
        _scenario_state["progress"].pop(prefix, None)
    else:
        live[key] = msg
        progress = _scenario_state["progress"].setdefault(prefix, _new_progress_entry())
        _update_progress_from_status_line(progress, msg)


def _scenario_should_stop():
    return _scenario_state.get("stop_requested", False)


def _scenario_should_pause():
    return _scenario_state.get("pause_requested", False)

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
    # steady-state Phase 1/2 chunk (_run_phase's own "N-M of TOTAL
    # iterations, writing every..." wording - TOTAL is the whole phase's
    # budget, not just this chunk's).
    re.compile(r"Running simpleFoam \(\d+-\d+ of (\d+) iterations, writing every"),
    re.compile(r"Running pimpleFoam to ([\d.]+)s"),  # decay transient run (single-run "Continue")
    # _finish_decay's own concurrent-launch announcement (single-run mode's
    # normal, non-Continue path - see _run_decay_pair) - only UV-on's own
    # "Time = N" lines ever reach _track_solver_time (see that function's
    # docstring), so this captures UV-on's own duration, not control's.
    # Missing this pattern entirely was a real, confirmed bug: target_time/
    # phase_start_time/chunk_base silently kept whatever flow convergence
    # left them at, so "Running now" froze at a stale flow-convergence
    # figure for the whole rest of the run even though the log kept
    # scrolling live Time=N lines the entire time.
    re.compile(r"Running pimpleFoam concurrently: UV-on \(([\d.]+)s\)"),
    # scenario_runs._run_shared_control/_run_scenario's own differently-
    # worded equivalents of the single-run decay line above (sweep mode).
    re.compile(r"Running pimpleFoam \(shared control, ([\d.]+)s\)"),
    re.compile(r"Running pimpleFoam \(UV-on, ([\d.]+)s\)"),
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
# Matches pimpleFoam's per-timestep "Time = N" banner line, not the ~8-10
# residual/Courant-number/continuity-error lines that follow it - used to
# throttle concurrent decay runs' visible log output (see
# app._run_decay_pair) down to one line per timestep instead of flooding
# with the full per-iteration dump for both runs at once.
_TIME_LINE_RE = re.compile(r"^Time\s*=\s*[\d.]+\s*$")


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
        start_time=time.time(), stop_requested=False, pause_requested=False,
        live_status={},
    )


def _complete_all_steps():
    for s in _run_state.get("steps", []):
        _run_state["step_status"][s] = "done"


def _should_stop():
    return _run_state.get("stop_requested", False)


def _should_pause():
    return _run_state.get("pause_requested", False)


_MAX_LOG_LINES = 5000


def _new_progress_entry():
    return {"target_time": None, "phase_start_time": None, "current_time": None, "chunk_base": None}


def _update_progress_from_log_line(progress, msg):
    """Extract target_time/phase_start_time/chunk_base from a phase-
    narration log line into `progress` (any dict shaped like
    _new_progress_entry()'s own target_time/phase_start_time/current_time/
    chunk_base fields) - shared by _run_log (single-run mode, updates
    _run_state directly) and _scenario_log (sweep mode, one small entry
    per ACH-group/combo key - see _scenario_state["progress"]) so both
    derive "Est. time to finish" the same way, from the same log lines the
    pipeline already emits (see _PHASE_TARGET_PATTERNS/_FLOW_BUDGET_RE/
    _FLOW_CHUNK_RE's own docstrings for why flow convergence needs the
    separate budget+chunk-base handling below instead of just being
    another entry in _PHASE_TARGET_PATTERNS).
    """
    m = _FLOW_BUDGET_RE.search(msg)
    if m:
        progress["target_time"] = float(m.group(1))
        progress["phase_start_time"] = time.time()
        progress["current_time"] = None
        progress["chunk_base"] = 0
        return
    m = _FLOW_CHUNK_RE.search(msg)
    if m:
        progress["chunk_base"] = float(m.group(1)) - 1
        return
    for pattern in _PHASE_TARGET_PATTERNS:
        m = pattern.search(msg)
        if m:
            progress["target_time"] = float(m.group(1))
            progress["phase_start_time"] = time.time()
            progress["current_time"] = None
            progress["chunk_base"] = None
            return


def _update_progress_from_status_line(progress, msg):
    """Extract current_time from a solver's raw "Time = N" status line into
    `progress` - shared by _track_solver_time (single-run) and
    _scenario_status_update (sweep mode). See
    _update_progress_from_log_line's own docstring for the shared dict
    shape both this and that function populate.
    """
    m = _TIME_RE.match(msg.strip())
    if m:
        base = progress.get("chunk_base")
        progress["current_time"] = str(float(m.group(1)) + base) if base is not None else m.group(1)


def _run_log(msg):
    msg = str(msg)
    log = _run_state["log"]
    log.append(msg)
    if len(log) > _MAX_LOG_LINES:
        # Streaming solver output line-by-line (vs. the old tail-20 dump)
        # means a long run can produce tens of thousands of lines - cap
        # memory growth while keeping plenty of scrollback.
        del log[: len(log) - _MAX_LOG_LINES]

    _update_progress_from_log_line(_run_state, msg)

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

    "[...]"-wrapped lines are the exception - run_wsl_streaming's own
    stall/retry diagnostics (see its docstring), not solver chatter -
    always forwarded to _run_log so a stalled/killed process is never
    silent just because this callback otherwise discards everything that
    isn't a "Time = N" line.
    """
    stripped = line.strip()
    if stripped.startswith("["):
        _run_log(stripped)
        return
    _update_progress_from_status_line(_run_state, stripped)


def _run_status_update(key, msg):
    """Single-run equivalent of _scenario_status_update - see
    _run_state["live_status"]'s own docstring for why this exists (decay
    mode's concurrent UV-on + UV-off control pair, see _run_decay_pair).
    msg=None removes that stream's entry (its solve just finished).
    """
    live = _run_state["live_status"]
    if msg is None:
        live.pop(key, None)
    else:
        live[key] = msg


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
    # Now called BEFORE setup_case() runs (see _run_decay/_run_steady_state)
    # so a run that fails or pauses before finishing still leaves this on
    # disk - which means it can no longer rely on setup_case()'s own
    # Path(case_dir).mkdir() having already run first, for a case directory
    # that's never been used before (confirmed as a real regression: a
    # brand-new case dir raised FileNotFoundError here instead of ever
    # reaching setup_case() at all).
    Path(case_dir).mkdir(parents=True, exist_ok=True)
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


def _write_setup_summary(case_dir, summary):
    """Persist setup_case()'s return value (fluence_mean, eACH_uv_well_mixed_mean,
    flow_converged, ach_delivery, etc.) right after it's computed, so a
    steady-state run that crashes anywhere inside run_steady_state_scenario()
    (Phase 1, Phase 2, or the bookkeeping between them - see
    steady_state_pipeline's own phase1_checkpoint) can be resumed later by
    calling _finish_steady_state() again WITHOUT re-running setup_case() -
    which would redo mesh generation and flow convergence from scratch,
    discarding the very state the crash happened downstream of. This is the
    general form of a real recovery done by hand once already (see
    steady_state_pipeline._write_phase1_checkpoint's docstring) - now the
    same idea applied one stage earlier.
    """
    with open(f"{case_dir}/setup_summary.json", "w") as f:
        json.dump(summary, f, indent=2)


def _read_setup_summary(case_dir):
    try:
        with open(f"{case_dir}/setup_summary.json") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _clear_setup_summary(case_dir):
    Path(f"{case_dir}/setup_summary.json").unlink(missing_ok=True)


def case_awaiting_phase2_resume(case_dir):
    """Whether `case_dir` holds a steady-state run whose setup_case() fully
    completed (setup_summary.json on disk) but the scenario never finished
    (no results.json yet) - Phase 2, or the bookkeeping right after it,
    crashed, was stopped, or the server was closed mid-run. Resuming skips
    straight to _finish_steady_state() reusing the persisted setup summary,
    and - if steady_state_pipeline's own phase1_checkpoint.json is also
    present - skips Phase 1 of the two-phase scenario too.

    Returns {"phase1_done": bool, "phase1_iterations": int or None} if
    there's something to resume, or None if there's no setup_summary, or the
    run already finished.
    """
    if not Path(f"{case_dir}/setup_summary.json").exists():
        return None
    if Path(f"{case_dir}/results.json").exists():
        return None
    phase1 = _read_phase1_checkpoint(case_dir)
    return {
        "phase1_done": phase1 is not None,
        "phase1_iterations": phase1["phase1_summary"]["iterations"] if phase1 else None,
    }


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
    # target-t-ss removed - no longer a UI field, see REFERENCE_TARGET_T_SS.
    "inject-x-input": "Injection X position", "inject-y-input": "Injection Y position",
    "inject-z-input": "Injection Z position", "source-zone-cells": "Source zone size (cells)",
    "breathing-velocity": "Breathing velocity", "breathing-dir-x": "Breathing direction X",
    "breathing-dir-y": "Breathing direction Y", "breathing-dir-z": "Breathing direction Z",
    "phase1-iterations": "Phase 1 iterations", "phase2-iterations": "Phase 2 iterations",
}


def _sealed_room_error(sim_type, ach_values, fan_enabled):
    """None if fine, else a user-facing message explaining why a "sealed
    room" (ACH<=0, no ventilation, mixing via fan only - see
    run_pipeline.setup_case's `sealed`) request can't run.

    ach_values: a single ach value or an iterable of them (a sweep's ACH
    list) - a sweep is rejected if ANY of its ACH values is sealed and the
    conditions below aren't met, same as a single run would be.
    """
    if isinstance(ach_values, (int, float)):
        ach_values = [ach_values]
    if not any(a <= 0 for a in ach_values):
        return None
    if sim_type != "decay":
        return ("Sealed-room / ACH<=0 is only supported in Decay mode - steady-state "
                "has no sensible zero-ventilation case.")
    if not fan_enabled:
        return ("Sealed room (ACH<=0) needs the mixing fan enabled - with no ventilation "
                "and no fan, there's no way for the flow field to develop.")
    return None


def _mechanical_ach_only_error(sim_type, ach_values, mech_ach_only):
    """None if fine, else a user-facing message explaining why a
    "mechanical ACH only" (no UV - see run_pipeline.setup_case's
    `mechanical_ach_only`) request can't run.

    ach_values: a single ach value or an iterable of them (a sweep's ACH
    list) - a sweep is rejected if ANY of its ACH values is <=0, same as a
    single run would be (this is the opposite constraint from sealed's own
    check - mechanical ACH only needs real ventilation to measure).
    """
    if not mech_ach_only:
        return None
    if isinstance(ach_values, (int, float)):
        ach_values = [ach_values]
    if sim_type != "decay":
        return ("Mechanical ACH only is only supported in Decay mode - steady-state has no "
                "sensible UV-free case.")
    if any(a <= 0 for a in ach_values):
        return ("Mechanical ACH only needs real ventilation (ACH>0) - a sealed room has no "
                "mechanical ventilation to measure.")
    return None


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
    adv = merge_project_openfoam_settings(settings, load_advanced_settings())
    # Recorded BEFORE setup_case() runs (not after) so that if flow
    # convergence stops without a verdict (FlowConvergenceUndecided) or the
    # run fails for any other reason, what was actually requested still
    # survives on disk - both for a resume (resume_case_setup needs the
    # SAME mesh-affecting settings _run_decay used) and for Continue's own
    # mismatch check, which previously silently assumed success.
    capture_openfoam_settings(settings, adv)
    _save_run_settings(case_dir, settings, guv_path=guv_path)
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
        pimple_delta_t=adv["pimple-delta-t"], max_co=adv["max-co"],
        cell_size=adv["mesh-cell-size"], nbins=adv["uv-zone-bins"],
        flow_rel_tol=adv["flow-rel-tol"] / 100.0, flow_max_iterations=adv["flow-max-iterations"],
        oscillation_window=adv["oscillation-window"], oscillation_growth_tol=adv["oscillation-growth-tol"],
        ach_delivery_tol=adv["ach-delivery-tol"] / 100.0,
        momentum_relaxation=adv["momentum-relaxation"], scalar_relaxation=adv["scalar-relaxation"],
        adaptive_t_relaxation=adv["adaptive-t-relaxation"],
        scalar_transport_ncorr=adv["scalar-transport-ncorr"],
        scalar_transport_tolerance=adv["scalar-transport-tolerance"],
        log_fn=_run_log, should_stop=_should_stop, solver_log_fn=_track_solver_time,
        should_pause=_should_pause,
        sealed=settings["ach"] <= 0, mechanical_ach_only=bool(settings.get("mech-ach-only")),
        **_fan_kwargs(settings),
        **_second_opening_kwargs(settings, "inlet2", room),
        **_second_opening_kwargs(settings, "outlet2", room),
    )
    if _should_stop():
        raise StoppedByUser("Stopped after case setup.")
    _finish_decay(case_dir, room, settings, summary)


def _decay_run_durations(ach, eACH_well_mixed_est, adv):
    """Adaptive pimpleFoam durations for the two decay-mode runs, per-run
    since they have separate targets (see ADVANCED_SETTINGS_DEFAULTS'
    decay-ach-min-fraction/decay-each-min-fraction/decay-each-max-fraction):

    - The UV-off control run only cares about ventilation's own (usually
      slow) removal rate, so it targets decay-ach-min-fraction (default
      90%) - pushing it to decay-each-max-fraction's higher target too
      would often cost hours for little gain (see the ACH=1 control run
      that needed ~9.5h of simulated time to reach 99.9% at a 0.73/hr
      measured rate, versus its UV-on twin needing only ~25 more minutes
      at 7.4/hr - the same physical time-constant problem that made
      steady-state's own Phase 1 slow).
    - The UV-on run targets decay-each-max-fraction (default 99.9%) when
      that's cheap - i.e. when the estimated time to reach it doesn't
      already exceed the practical ceiling below - and falls back to
      decay-each-min-fraction otherwise. "cheap" is operationalized
      exactly this way (not a separate arbitrary "eACH is high" threshold)
      since the two are equivalent for a first-order decay: a high combined
      rate IS what makes a high log-reduction target cheap to reach.

    ceiling matches the GUI's own pimple-end-time slider max (7200s) - the
    only existing "this is impractically long" convention already in use.
    """
    ceiling = 7200
    control_end_time = _settling_iterations(
        ach, target_fraction=adv["decay-ach-min-fraction"] / 100.0, max_iterations=ceiling)

    combined_rate = ach + eACH_well_mixed_est
    raw_each_max_time = _settling_iterations(
        combined_rate, target_fraction=adv["decay-each-max-fraction"] / 100.0, max_iterations=10 ** 9)
    each_target = (adv["decay-each-max-fraction"] if raw_each_max_time <= ceiling
                   else adv["decay-each-min-fraction"])
    combined_end_time = _settling_iterations(combined_rate, target_fraction=each_target / 100.0,
                                              max_iterations=ceiling)
    return combined_end_time, control_end_time


def _extend_decay_if_short_dash(case_dir, label, target_fraction, end_time, adv, write_interval):
    """DECAY MODE ONLY: continue an already-finished pimpleFoam decay until it
    actually reaches its reduction target, re-measuring the rate from its own
    curve. Dash-side twin of the Qt/sweep helpers - see
    decay_analysis.run_decay_to_target. No-op when decay-extend-to-target is
    off or the target was already met.
    """
    if not adv.get("decay-extend-to-target", True):
        return None
    case_wsl = wsl_path(case_dir)

    def run_fn(_end):
        return run_wsl_streaming("pimpleFoam 2>&1 | tee -a log.pimpleFoam", case_wsl,
                                  on_line=_track_solver_time, should_stop=_should_stop,
                                  kill_pattern="pimpleFoam", should_pause=_should_pause)

    def set_end_time_fn(new_end):
        set_control_dict_start_from(case_dir, "latestTime")
        set_control_dict_time(case_dir, end_time=new_end, write_interval=write_interval,
                               delta_t=adv["pimple-delta-t"], max_co=adv["max-co"])

    first = {"done": False}

    def run_or_skip(e):
        if not first["done"]:                 # the initial solve already happened
            first["done"] = True
            return None
        return run_fn(e)

    _, info = run_decay_to_target(
        label, target_fraction, end_time, adv.get("decay-max-total-time", 7200),
        run_or_skip, set_end_time_fn,
        lambda: read_decay_curve(case_dir),
        log_fn=_run_log)
    return info


def _run_decay_pair(case_dir_wsl, control_dir_wsl, combined_end_time=None, control_end_time=None):
    """Run the UV-on and (if control_dir_wsl is given) UV-off-control
    pimpleFoam solves CONCURRENTLY - both only depend on the shared,
    already-converged flow field prepared before this point, so from here
    on they're fully independent (see the ACH=1/Z=1.7 vs ACH=6/Z=7
    comparisons this session: ~510s concurrent wall-clock for both runs
    together, vs. what would be roughly double running them back to back).

    control_dir_wsl=None (a sealed room - see setup_case's `sealed`) skips
    the control run entirely instead of launching a 2nd thread for it -
    with no ventilation, its own measured baseline is exactly (not just
    approximately) 0 by construction, so running a whole 2nd pimpleFoam
    solve to confirm that would be pure wasted compute, not a real
    measurement (see _finish_decay's own docstring for the full reasoning).

    Neither stream's "Time = N" lines reach the scrolling log - both feed
    _run_state["live_status"] instead (see _run_status_update), which
    "Running now" renders live, keyed by stream name - appending both
    concurrent streams' lines to the log the way an earlier version of
    this function did flooded it fast enough to scroll real narration
    (step transitions, convergence summaries, errors) out of view within
    seconds. Only the main (UV-on) run's output additionally feeds
    _track_solver_time (drives the single-run ETA estimate, which assumes
    one linear sequence - interleaving control's own "Time = N" progress
    into that same tracker would produce a nonsensical, jumping ETA).

    should_stop is shared (the single global _should_stop()) - stopping
    the run is meant to stop the whole scenario (both curves), not just
    one, so both threads killing on the same "pimpleFoam" pattern is the
    intended behavior here, not the cross-case-collision risk that pattern
    would have for genuinely independent, concurrently-running scenarios
    (see the deferred scenario-run parallelization proposal).
    """
    results = {}
    errors = {}

    def run_one(name, cwd_wsl, on_line, log_prefix, total_time):
        try:
            callback = scenario_runs._throttled_solver_callback(
                _run_log, log_prefix, on_line=on_line, status_fn=_run_status_update, status_key=log_prefix,
                total_time=total_time)
            results[name] = run_wsl_streaming(
                "pimpleFoam 2>&1 | tee log.pimpleFoam", cwd_wsl,
                on_line=callback, should_stop=_should_stop, kill_pattern="pimpleFoam",
                should_pause=_should_pause,
            )
        except Exception as e:
            errors[name] = e
        finally:
            _run_status_update(log_prefix, None)

    threads = [threading.Thread(target=run_one,
                                 args=("uv", case_dir_wsl, _track_solver_time, "UV-on", combined_end_time))]
    if control_dir_wsl is not None:
        threads.append(threading.Thread(target=run_one,
                                         args=("control", control_dir_wsl, None, "control", control_end_time)))
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    if errors:
        raise next(iter(errors.values()))
    return results["uv"], results.get("control")


def _finish_decay(case_dir, room, settings, summary):
    """Everything _run_decay() does after setup_case() returns a summary -
    factored out so the flow-convergence-decision resume path (see
    _resume_pipeline_thread) can reach the exact same steps after
    resume_case_setup() resolves a previously-undecided flow convergence,
    without repeating (or having to keep in sync by hand) the pimpleFoam
    run/results-writing/monitoring logic below.

    The UV-off control run is no longer optional (see the removed
    "no-uv-control-enable" checkbox) - decay mode is now the default path
    (steady-state is frozen, kept only for illustration/cross-checks), and
    the measured-ACH correction it provides turned out to matter a lot in
    practice (a ~35% mixing-efficiency gap showed up on a real case this
    session) - cheap enough now that it always runs concurrently with the
    main UV-on solve.

    Sealed rooms (ach<=0 - see setup_case's `sealed`) are the one
    exception: the control run is skipped entirely, not just made
    optional. With every opening closed off, there is no possible path for
    contaminant MASS to leave the room at all - a mixing fan can only
    redistribute it, not remove it - so the true ventilation-only decay
    rate a control run would measure is exactly (not just approximately)
    zero, by construction, regardless of what a real CFD solve of it would
    report (numerical/discretization drift only). Running a full 2nd
    concurrent pimpleFoam solve to confirm a value already known
    analytically would double this run's compute cost for no new
    information - `measured_ventilation_ach=0.0` below is passed as an
    exact value, not an estimate, and is strictly more correct than
    whatever noisy near-zero number an actual control run would measure.
    """
    adv = merge_project_openfoam_settings(settings, load_advanced_settings())
    case_dir_wsl = wsl_path(case_dir)
    sealed = settings["ach"] <= 0
    mech_ach_only = bool(settings.get("mech-ach-only"))
    skip_control = sealed or mech_ach_only
    control_dir = f"{case_dir}/no_UV"
    control_dir_wsl = wsl_path(control_dir)

    combined_end_time, control_end_time = _decay_run_durations(
        settings["ach"], summary["eACH_uv_well_mixed_mean"], adv)
    write_interval = max(1, settings["pimple-write-interval"])
    if skip_control:
        reason = "sealed room" if sealed else "mechanical ACH only"
        _run_log(f"Adaptive run duration: UV-on={combined_end_time}s ({reason} - no UV-off control "
                 f"needed, see below), write interval={write_interval}s (as configured)...")
    else:
        _run_log(f"Adaptive run durations: UV-on={combined_end_time}s, UV-off control={control_end_time}s "
                 f"(targets: eACH {adv['decay-each-min-fraction']:.3g}-{adv['decay-each-max-fraction']:.3g}%, "
                 f"ACH {adv['decay-ach-min-fraction']:.3g}%), write interval={write_interval}s (as configured)...")
    set_control_dict_time(case_dir, end_time=combined_end_time,
                           write_interval=write_interval, delta_t=adv["pimple-delta-t"], max_co=adv["max-co"])
    splice_live_vol_average_if_needed(case_dir)

    if adv["t-clamp-decay-enabled"]:
        # Decay mode carves no source zone (T starts uniform at
        # REFERENCE_TARGET_T_SS with no injection during the UV-on run,
        # only removal) - the physical ceiling is that known starting
        # value itself, not a converged-field lookup like steady-state's
        # source_zone_max_T (see tclamp_decay.py's module docstring).
        # Spliced before the UV-off control is cloned below (harmless
        # there too - no injection, T only decreases from the same start).
        ensure_tclamp_decay_compiled(_run_log)
        splice_tclamp_decay_if_needed(case_dir, adv["t-clamp-decay-multiplier"] * REFERENCE_TARGET_T_SS)

        # DECAY MODE: T must be solved essentially unrelaxed. In a transient run
        # the ddt term supplies the stability that under-relaxation supplies in a
        # steady one, so relaxing here stabilises nothing - it only stops each
        # timestep reaching the implicit solution, applying the UV sink at a
        # fraction of its strength every step. Measured on v9: T=0.05 gave
        # 4.80 /hr against a 72.17 /hr well-mixed prediction; T=1.0 gave
        # 70.74 /hr. The steady-state value is left untouched - Phase 2 needs it.
        _decay_relax = adv.get("decay-scalar-relaxation", 1.0)
        _run_log(f"Decay mode: setting T under-relaxation to {_decay_relax} "
               f"(transient - the time term provides stability, not relaxation)...")
        set_relaxation_factors(case_dir, scalar_factor=_decay_relax)

    if _should_stop():
        raise StoppedByUser("Stopped before pimpleFoam.")
    if sealed:
        _run_log("Skipping the UV-off control run - sealed room, no possible path for contaminant "
                 "mass to leave (a fan redistributes air but can't remove contaminant from a closed "
                 "room), so the true ventilation-only decay rate is exactly 0 by construction.")
    elif mech_ach_only:
        _run_log("Skipping the UV-off control run - mechanical ACH only, this run's own decay curve "
                 "(empty UV source) already IS the ventilation-only measurement.")
    else:
        _run_log("=== Preparing UV-off control (subfolder \"no_UV\") - clone before either pimpleFoam run ===")
        # The occupant is breathing in the UV-off case too, and the
        # constraint measurably alters the flow field this run measures the
        # ventilation rate ON - see scenario_runs._carve_breathing_inlet.
        _bv = breathing_inlet_velocity(settings)
        control_breathing_entry = (
            breathing_inlet_velocity_constraint(zone_name="sourceZone", velocity_magnitude=_bv,
                                                direction=breathing_inlet_direction(settings))
            if _bv > 0 else None)
        prepare_ventilation_only_control(
            case_dir, control_dir, summary["inlet_velocity"],
            control_end_time, write_interval, pimple_delta_t=adv["pimple-delta-t"], max_co=adv["max-co"],
            inlet2_velocity=summary.get("inlet2_velocity") if settings.get("inlet2-enable") else None,
            has_outlet2=bool(settings.get("outlet2-enable")),
            sealed=False,
            log_fn=_run_log, should_stop=_should_stop,
            breathing_entry=control_breathing_entry,
            fan_entry=fan_entry_from_settings(settings),
            scalar_relaxation=adv.get("decay-scalar-relaxation", 1.0),
        )
        if control_breathing_entry is not None:
            scenario_runs._carve_breathing_inlet(control_dir, room, settings, adv, _run_log)

    if skip_control:
        _run_log(f"Running pimpleFoam: UV-on ({combined_end_time}s)...")
        r_uv, _ = _run_decay_pair(case_dir_wsl, None, combined_end_time=combined_end_time)
    else:
        _run_log(f"Running pimpleFoam concurrently: UV-on ({combined_end_time}s) + "
                 f"UV-off control ({control_end_time}s)...")
        r_uv, r_control = _run_decay_pair(case_dir_wsl, control_dir_wsl,
                                           combined_end_time=combined_end_time, control_end_time=control_end_time)
    if _should_stop():
        raise StoppedByUser("Stopped during pimpleFoam.")
    runs_to_check = [("UV-on", r_uv)] if skip_control else [("UV-on", r_uv), ("UV-off control", r_control)]
    for label, r in runs_to_check:
        if r.returncode != 0 or "FOAM FATAL" in r.stdout or "Floating Point Exception" in r.stdout:
            tail = "\n".join(r.stdout.splitlines()[-25:]) or "(no output captured)"
            raise RuntimeError(f"{label} pimpleFoam failed (exit {r.returncode}):\n{tail}")

    # DECAY MODE ONLY: both durations came from ASSUMED rates - re-check each
    # against the rate actually achieved and continue if short.
    _cheap = combined_end_time <= adv.get("decay-max-total-time", 7200)
    _extend_decay_if_short_dash(
        case_dir, "UV-on",
        (adv["decay-each-max-fraction"] if _cheap else adv["decay-each-min-fraction"]) / 100.0,
        combined_end_time, adv, write_interval)
    if not skip_control:
        _extend_decay_if_short_dash(control_dir, "control",
                                     adv["decay-ach-min-fraction"] / 100.0,
                                     control_end_time, adv, write_interval)

    _run_log("Computing spatial coefficient of variation (final concentration field, "
             "across all cells - how uniformly the room actually cleared, not just on average)...")
    try:
        spatial_cov = spatial_coefficient_of_variation(read_latest_time_field(case_dir, "T"))
    except Exception as e:
        _run_log(f"  Could not compute spatial CoV: {e}")
        spatial_cov = None

    if sealed:
        control_results = {"total_ach_actual": 0.0}
    elif mech_ach_only:
        control_results = None  # no separate control run - this run's own curve IS the measurement
    else:
        _run_log("Writing results summary...")
        write_results_summary(
            case_dir, f"{case_dir}/results.json", settings["ach"],
            summary["eACH_uv_well_mixed_mean"],
            extra={
                "n_lamps": summary["n_lamps"], "fluence_mean": summary["fluence_mean"],
                "flow_converged": summary.get("flow_converged"), "ach_delivery": summary.get("ach_delivery"),
                "spatial_cov_final": spatial_cov,
            },
        )
        _run_log("=== Post-processing UV-off control ===")
        control_results = finish_ventilation_only_control(control_dir, settings["ach"], log_fn=_run_log)

    if mech_ach_only:
        _run_log("Writing results summary (mechanical ACH only - no separate control run to "
                 "correct against; total_ach_actual is this run's own measured rate)...")
        results = write_results_summary(
            case_dir, f"{case_dir}/results.json", settings["ach"], 0.0,
            extra={
                "n_lamps": summary["n_lamps"], "fluence_mean": summary["fluence_mean"],
                "flow_converged": summary.get("flow_converged"), "ach_delivery": summary.get("ach_delivery"),
                "spatial_cov_final": spatial_cov,
            },
        )
    else:
        _run_log("Writing results summary..." if sealed else
                 "Updating results.json with corrected mixing efficiency (measured, "
                 "not nominal, ventilation ACH)...")
        results = write_results_summary(
            case_dir, f"{case_dir}/results.json", settings["ach"],
            summary["eACH_uv_well_mixed_mean"],
            extra={
                "n_lamps": summary["n_lamps"], "fluence_mean": summary["fluence_mean"],
                "flow_converged": summary.get("flow_converged"), "ach_delivery": summary.get("ach_delivery"),
                "spatial_cov_final": spatial_cov,
            },
            measured_ventilation_ach=control_results["total_ach_actual"],
            measured_ventilation_ach_ci95=control_results.get("total_ach_actual_ci95"),
            measured_ventilation_ach_se_per_s=control_results.get("fit_se_per_s"),
            measured_ventilation_fit_dof=(control_results["fit_n"] - 2) if control_results.get("fit_n") else None,
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
    _run_log(f"Done. eACH_uv effective={results['eACH_uv_assuming_well_mixed']:.4g} /hr "
             f"(well-mixed={results['eACH_uv_well_mixed']:.4g} /hr)")
    if "mixing_efficiency_actual" in results:
        _run_log(f"  Corrected mixing efficiency (measured ventilation baseline): "
                 f"{results['mixing_efficiency_actual'] * 100:.1f}% "
                 f"(vs {results['mixing_efficiency'] * 100:.1f}% using nominal ACH)")


def _continue_decay(case_dir, end_time, write_interval):
    """Thin wrapper over scenario_runs.continue_decay, supplying this app's
    own log_fn/should_stop/should_pause/solver_log_fn (moved there
    2026-08-10 so the Qt app's own "Extend / modify simulations" flow can
    call the identical logic instead of reimplementing it - see that
    function's own docstring for the full behavior)."""
    results = scenario_runs.continue_decay(
        case_dir, end_time, write_interval,
        log_fn=_run_log, should_stop=_should_stop, should_pause=_should_pause,
        solver_log_fn=_track_solver_time,
    )
    _complete_all_steps()
    return results


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
    if settings["ach"] <= 0:
        raise ValueError("Sealed-room / ACH<=0 is only supported in Decay mode - "
                          "steady-state has no sensible zero-ventilation case.")
    adv = merge_project_openfoam_settings(settings, load_advanced_settings())
    fan_kwargs = _fan_kwargs(settings)

    # Recorded BEFORE setup_case() runs - see _run_decay's identical comment.
    capture_openfoam_settings(settings, adv)
    _save_run_settings(case_dir, settings, guv_path=guv_path)

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
        oscillation_window=adv["oscillation-window"], oscillation_growth_tol=adv["oscillation-growth-tol"],
        ach_delivery_tol=adv["ach-delivery-tol"] / 100.0,
        momentum_relaxation=adv["momentum-relaxation"], scalar_relaxation=adv["scalar-relaxation"],
        adaptive_t_relaxation=adv["adaptive-t-relaxation"],
        scalar_transport_ncorr=adv["scalar-transport-ncorr"],
        scalar_transport_tolerance=adv["scalar-transport-tolerance"],
        max_co=adv["max-co"],
        log_fn=_run_log, should_stop=_should_stop, solver_log_fn=_track_solver_time,
        should_pause=_should_pause,
        **fan_kwargs,
        **_second_opening_kwargs(settings, "inlet2", room),
        **_second_opening_kwargs(settings, "outlet2", room),
    )
    if _should_stop():
        raise StoppedByUser("Stopped after case setup.")
    _finish_steady_state(case_dir, room, settings, summary)


def _finish_steady_state(case_dir, room, settings, summary,
                          phase1_resume_decision=None, phase1_resume_additional_iterations=None):
    """Everything _run_steady_state() does after setup_case() returns a
    summary - factored out so the flow-convergence-decision resume path
    (see _resume_pipeline_thread) can reach the exact same steps after
    resume_case_setup() resolves a previously-undecided flow convergence,
    without repeating (or having to keep in sync by hand) the Phase 1/
    Phase 2 orchestration below.

    phase1_resume_decision/phase1_resume_additional_iterations: set only
    when resuming a Phase1ExtrapolationUndecided pause (see
    _resume_phase1_extrapolation_thread) - passed straight through to
    run_steady_state_scenario, which reuses Phase 1's already-mid-run
    state (0/, source cellZone, fvOptions) instead of restarting it.
    """
    # Persisted here (not just held in this call's summary argument) so a
    # crash anywhere below - inside run_steady_state_scenario() or in the
    # results.json bookkeeping after it - can be resumed from a fresh
    # server session via case_awaiting_phase2_resume(), without re-running
    # setup_case()'s mesh generation and flow convergence from scratch.
    _write_setup_summary(case_dir, summary)

    adv = merge_project_openfoam_settings(settings, load_advanced_settings())
    fan_kwargs = _fan_kwargs(settings)
    fan_entry = None
    if settings["fan-enable"]:
        fan_entry = fan_fvoptions_entry(settings["fan-speed"], direction=fan_kwargs["fan_direction"])

    room_volume = room.x * room.y * room.z
    cell_size = adv["mesh-cell-size"]
    openings = [(settings["inlet-wall"],
                 opening_actual_area(settings["inlet-wall"], room.x, room.y, room.z,
                                      _opening_center_frac(settings, "inlet", room),
                                      (settings["inlet-size-w"], settings["inlet-size-h"]), cell_size))]
    has_inlet2 = bool(settings.get("inlet2-enable"))
    if has_inlet2:
        openings.append((settings["inlet2-wall"],
                          opening_actual_area(settings["inlet2-wall"], room.x, room.y, room.z,
                                               _opening_center_frac(settings, "inlet2", room),
                                               (settings["inlet2-size-w"], settings["inlet2-size-h"]), cell_size)))
    velocities = compute_inlet_velocities(settings["ach"], room_volume, openings)
    inlet_velocity = velocities[0]
    inlet2_velocity = velocities[1] if has_inlet2 else None
    has_outlet2 = bool(settings.get("outlet2-enable"))

    ach = settings["ach"]
    eACH_uv = summary.get("eACH_uv_well_mixed_mean", 0.0)
    deltat_adv = merge_project_deltat_settings(settings, adv)
    if deltat_adv["deltat-scaling-enabled"]:
        # deltaT scaling and the settling-safety-multiplier below solve the
        # exact same equation (residence times needed within a budget) for
        # the opposite unknown - composing them is redundant and self-
        # defeating: inflating the iteration count first (to whatever the
        # multiplier estimates) leaves deltaT nothing left to do, since that
        # inflated count already covers the same target span at deltaT=1.
        # Confirmed directly: at ACH=6 the multiplier inflates a configured
        # 1500-iteration budget to ~7948, silently discarding the user's
        # actual requested (and, with deltaT scaling, sufficient) budget -
        # use it as configured instead, and let deltaT provide coverage.
        phase1_iterations = settings["phase1-iterations"]
        phase2_iterations = settings["phase2-iterations"]
    else:
        # Phase 1's naive settling estimate (well-mixed idealization) has been
        # confirmed to run short of where extrapolation actually stabilizes -
        # a real case's fitted tau came out ~3x the naive theoretical tau at
        # the slowest tested ACH. Applying a safety multiplier gives a more
        # realistic *starting* budget - not a hard cap either way, since
        # phase1_extrapolation_gate can still raise Phase1ExtrapolationUndecided
        # and offer to extend further if even this isn't enough.
        phase1_settling_estimate = round(_settling_iterations(ach) * adv["phase1-settling-safety-multiplier"])
        # Clamped to the configured ceiling - this is a *starting* budget, not
        # a promise Phase 1 will actually need this many iterations (the
        # extrapolation gate below stops as soon as it's confident either way);
        # the ceiling exists so a first attempt never silently launches a much
        # bigger run than the user configured as their hard backstop.
        phase1_iterations = min(max(settings["phase1-iterations"], phase1_settling_estimate),
                                 adv["phase1-max-iterations-ceiling"])
        phase2_iterations = max(settings["phase2-iterations"], _settling_iterations(ach + eACH_uv))
        _run_log(f"Settling estimate: phase1={phase1_settling_estimate} iterations (ACH={ach:.3g}/hr alone, "
                 f"{adv['phase1-settling-safety-multiplier']:.1f}x safety margin, capped at "
                 f"{adv['phase1-max-iterations-ceiling']}), "
                 f"phase2={_settling_iterations(ach + eACH_uv)} iterations (ACH+eACH_uv={ach + eACH_uv:.3g}/hr) - "
                 f"using the larger of this and the configured value for each phase "
                 f"({phase1_iterations}, {phase2_iterations}).")
    phase1_delta_t, phase2_delta_t = resolve_phase_delta_ts(ach, eACH_uv, phase1_iterations, phase2_iterations,
                                                             deltat_adv)
    if phase1_delta_t != 1 or phase2_delta_t != 1:
        _run_log(f"Residence-time-scaled deltaT: phase1={phase1_delta_t}, phase2={phase2_delta_t}, "
                 f"iterations phase1={phase1_iterations}/phase2={phase2_iterations} (as configured, not "
                 f"settling-estimate-inflated - deltaT scaling replaces that mechanism when enabled).")

    patches_to_monitor = ("outlet", "outlet2") if has_outlet2 else ("outlet",)
    result = run_steady_state_scenario(
        case_dir, room.x, room.y, room.z, settings["ach"], settings["z-value"],
        source_center=(settings["inject-x-input"], settings["inject-y-input"], settings["inject-z-input"]),
        target_T_ss=REFERENCE_TARGET_T_SS,
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
        phase1_write_interval=adv["phase-write-interval"],
        phase2_write_interval=adv["phase-write-interval"],
        window_frac=settings.get("t-ss-window-frac") or 0.15,
        cell_size=adv["mesh-cell-size"], nbins=adv["uv-zone-bins"],
        source_size=resolve_source_size(settings, adv["mesh-cell-size"], (room.x, room.y, room.z)),
        plateau_rel_tol=adv["plateau-rel-tol"] / 100.0,
        mass_balance_tol=adv["mass-balance-tol"] / 100.0,
        # GUI-exposed cross-project "advanced" default (Settings menu) - only
        # meaningful when t_inf_rel_tol is actually set below, since
        # _run_phase defaults check_interval to the whole phase (a no-op
        # single chunk) otherwise.
        t_inf_check_interval=adv["phase-chunk-size"] if adv["t-infinity-early-stop-enabled"] else None,
        t_inf_rel_tol=(adv["t-infinity-rel-tol"] / 100.0) if adv["t-infinity-early-stop-enabled"] else None,
        t_inf_streak=adv["phase1-extrapolation-streak"],
        keep_all_timesteps=adv["keep-all-timesteps"],
        phase1_t_initial=adv["phase1-t-initial"],
        phase1_extrapolation_gate=adv["phase1-require-stable-extrapolation"],
        phase1_resume_decision=phase1_resume_decision,
        phase1_resume_additional_iterations=phase1_resume_additional_iterations,
        fan_entry=fan_entry, monitoring_points=_gather_monitoring_points(settings),
        patches_to_monitor=patches_to_monitor,
        log_fn=_run_log, should_stop=_should_stop, solver_log_fn=_track_solver_time,
        should_pause=_should_pause,
        phase1_delta_t=phase1_delta_t, phase2_delta_t=phase2_delta_t,
        t_clamp_decay_multiplier=adv["t-clamp-decay-multiplier"] if adv["t-clamp-decay-enabled"] else None,
        phase1_tmax_multiplier=adv["phase1-tmax-multiplier"] if adv["t-clamp-decay-enabled"] else None,
        breathing_inlet_velocity=breathing_inlet_velocity(settings),
        breathing_inlet_dir=breathing_inlet_direction(settings),
    )
    result["fluence_mean"] = summary["fluence_mean"]
    result["eACH_uv_well_mixed"] = summary.get("eACH_uv_well_mixed_mean")
    # Flow/ACH-delivery trust status - computed once during setup_case(),
    # carried into this scenario's own results.json so a report/GUI badge
    # can reflect them alongside phase1/phase2's own T-convergence status
    # (see steady_state_pipeline.check_mass_balance) without re-deriving
    # anything - these three are deliberately kept as separate signals
    # (see the flow-vs-T trust-status discussion), not blended into one.
    result["flow_converged"] = summary.get("flow_converged")
    result["ach_delivery"] = summary.get("ach_delivery")
    result["mechanical_mixing_efficiency_pct"] = mechanical_mixing_efficiency_pct(result)
    try:
        snapshot_openfoam_settings(case_dir)
    except Exception:
        pass  # archival only - never block a results.json write over it
    with open(f"{case_dir}/results.json", "w") as f:
        json.dump(result, f, indent=2)
    # Finished end-to-end - nothing left to resume.
    _clear_setup_summary(case_dir)
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
        results = migrate_result_keys(json.load(f))  # pre-2026-09-02 results.json files use the old key names
    results["run_started_at"] = started_at.isoformat()
    results["run_elapsed_seconds"] = elapsed_seconds
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)


def _handle_flow_convergence_undecided(e, sim_type, guv_path, case_dir, room, settings, started_at, start,
                                        kind="flow"):
    """Shared by _run_pipeline_thread and _resume_pipeline_thread (a
    "continue N more iterations" attempt can perfectly well land on
    FlowConvergenceUndecided again, exactly like a fresh attempt would -
    same handling either way: never a dead-end error, always a decision
    with real data behind it (see the diagnostic dict's own docstring in
    run_pipeline._oscillation_diagnostic).

    kind: "flow" (FlowConvergenceUndecided) or "phase1_extrapolation"
    (steady_state_pipeline.Phase1ExtrapolationUndecided) - both pause on
    the exact same Processing-tab decision panel (continue/accept/stop),
    since the UX is identical either way; _start_flow_decision dispatches
    to the right resume thread based on this field.
    """
    label = "Flow convergence" if kind == "flow" else "Phase 1"
    _run_log(f"{label} paused after {e.total_iterations} iterations - awaiting your decision. "
             f"{e.diagnostic['summary']}")
    _run_state["status"] = "awaiting_decision"
    _run_state["decision"] = {
        "sim_type": sim_type, "guv_path": guv_path, "case_dir": case_dir, "room": room, "settings": settings,
        "diagnostic": e.diagnostic, "total_iterations": e.total_iterations,
        "started_at": started_at, "start": start, "kind": kind,
    }


def _write_single_run_summary_csv(case_dir):
    """Single-run equivalent of scenario_runs.write_sweep_summary_csv - a
    sweep collects every combo's own compound-named report.json from the
    project directory, but a single run never writes one of those (its
    results.json lives directly in case_dir, one run per directory) - so
    this reads that instead and writes the same 5-metric CSV, for the same
    reason: comparing without needing to open results.json by hand.
    """
    try:
        with open(f"{case_dir}/results.json") as f:
            detail = migrate_result_keys(json.load(f))  # pre-2026-09-02 results.json files use the old key names
    except (FileNotFoundError, json.JSONDecodeError):
        return
    metrics = combo_summary_metrics(detail)
    csv_path = f"{case_dir}/run_summary.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["Z", "ACH", *metrics.keys()])
        writer.writeheader()
        writer.writerow({"Z": _run_state.get("z"), "ACH": _run_state.get("ach"), **metrics})


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
        _write_single_run_summary_csv(case_dir)
    except StoppedByUser as e:
        _run_log(f"Stopped: {e}")
        _run_state["status"] = "stopped"
    except FlowConvergenceUndecided as e:
        _handle_flow_convergence_undecided(e, sim_type, guv_path, case_dir, room, settings, started_at, start)
    except Phase1ExtrapolationUndecided as e:
        _handle_flow_convergence_undecided(e, sim_type, guv_path, case_dir, room, settings, started_at, start,
                                            kind="phase1_extrapolation")
    except Exception as e:
        _run_log(f"ERROR: {e}")
        _run_state["status"] = "error"


def _resume_pipeline_thread(action, additional_iterations):
    """Runs after the user clicks Continue/Accept on the Processing tab's
    decision panel (see FlowConvergenceUndecided/_run_pipeline_thread) -
    resumes flow convergence (or accepts it as-is) via resume_case_setup(),
    then falls through to the exact same _finish_decay/_finish_steady_state
    logic a normal run would have reached, using the settings/room recorded
    at the moment flow convergence first paused (not whatever the GUI form
    happens to show now - the button click already re-validated those
    against run_settings.json before launching this thread, see
    _start_flow_decision's _settings_mismatch check).
    """
    decision = _run_state["decision"]
    sim_type, guv_path, case_dir = decision["sim_type"], decision["guv_path"], decision["case_dir"]
    room, settings = decision["room"], decision["settings"]
    started_at, start = decision["started_at"], decision["start"]
    adv = merge_project_openfoam_settings(settings, load_advanced_settings())
    fan_kwargs = _fan_kwargs(settings)
    has_inlet2 = bool(settings.get("inlet2-enable"))
    has_outlet2 = bool(settings.get("outlet2-enable"))

    try:
        summary = resume_case_setup(
            case_dir, guv_path, action,
            ach=settings["ach"], Z=settings["z-value"], nbins=adv["uv-zone-bins"],
            inlet_wall=settings["inlet-wall"], inlet_center=_opening_center_frac(settings, "inlet", room),
            inlet_size=(settings["inlet-size-w"], settings["inlet-size-h"]),
            inlet_diffuser_type=settings.get("inlet-diffuser-type", "direct"),
            inlet2_wall=settings["inlet2-wall"] if has_inlet2 else None,
            inlet2_center=_opening_center_frac(settings, "inlet2", room) if has_inlet2 else None,
            inlet2_size=(settings["inlet2-size-w"], settings["inlet2-size-h"]) if has_inlet2 else None,
            inlet2_diffuser_type=settings.get("inlet2-diffuser-type", "direct") if has_inlet2 else "direct",
            outlet2_wall=settings["outlet2-wall"] if has_outlet2 else None,
            cell_size=adv["mesh-cell-size"], additional_iterations=additional_iterations,
            flow_rel_tol=adv["flow-rel-tol"] / 100.0,
            oscillation_window=adv["oscillation-window"], oscillation_growth_tol=adv["oscillation-growth-tol"],
            ach_delivery_tol=adv["ach-delivery-tol"] / 100.0,
            pimple_end_time=settings.get("pimple-end-time", 120),
            pimple_write_interval=settings.get("pimple-write-interval", 10),
            pimple_delta_t=adv["pimple-delta-t"], max_co=adv["max-co"],
            fan_speed=fan_kwargs.get("fan_speed"), fan_direction=fan_kwargs.get("fan_direction", (0, 0, -1)),
            mechanical_ach_only=bool(settings.get("mech-ach-only")),
            log_fn=_run_log, should_stop=_should_stop, solver_log_fn=_track_solver_time,
            should_pause=_should_pause,
        )
        if sim_type == "decay":
            _finish_decay(case_dir, room, settings, summary)
        else:
            _finish_steady_state(case_dir, room, settings, summary)
        _run_state["status"] = "done"
        _record_run_timing(case_dir, started_at, time.time() - start)
    except StoppedByUser as e:
        _run_log(f"Stopped: {e}")
        _run_state["status"] = "stopped"
    except FlowConvergenceUndecided as e:
        _handle_flow_convergence_undecided(e, sim_type, guv_path, case_dir, room, settings, started_at, start)
    except Phase1ExtrapolationUndecided as e:
        _handle_flow_convergence_undecided(e, sim_type, guv_path, case_dir, room, settings, started_at, start,
                                            kind="phase1_extrapolation")
    except Exception as e:
        _run_log(f"ERROR: {e}")
        _run_state["status"] = "error"


def _resume_phase1_extrapolation_thread(action, additional_iterations):
    """Runs after the user clicks Continue/Accept on the Processing tab's
    decision panel for a Phase1ExtrapolationUndecided pause. Unlike flow
    convergence's resume (which needs resume_case_setup() to redo mesh-
    stage bookkeeping), Phase 1's own mesh/flow/setup already fully
    succeeded - that's why Phase 1 itself got to run at all - so this just
    re-enters _finish_steady_state() with phase1_resume_decision set,
    reusing the persisted setup_summary.json exactly like
    case_awaiting_phase2_resume's own resume path does.
    """
    decision = _run_state["decision"]
    case_dir, room, settings = decision["case_dir"], decision["room"], decision["settings"]
    started_at, start = decision["started_at"], decision["start"]
    try:
        summary = _read_setup_summary(case_dir)
        _finish_steady_state(case_dir, room, settings, summary,
                              phase1_resume_decision=action, phase1_resume_additional_iterations=additional_iterations)
        _run_state["status"] = "done"
        _record_run_timing(case_dir, started_at, time.time() - start)
    except StoppedByUser as e:
        _run_log(f"Stopped: {e}")
        _run_state["status"] = "stopped"
    except Phase1ExtrapolationUndecided as e:
        _handle_flow_convergence_undecided(e, decision["sim_type"], decision["guv_path"], case_dir, room, settings,
                                            started_at, start, kind="phase1_extrapolation")
    except Exception as e:
        _run_log(f"ERROR: {e}")
        _run_state["status"] = "error"


def _resume_phase2_thread():
    """Runs after the user clicks Resume on the Processing tab's phase2-
    resume panel (see case_awaiting_phase2_resume) - skips setup_case()
    entirely and jumps straight to _finish_steady_state() using the
    setup_summary.json persisted by an earlier attempt, reusing the mesh,
    flow field, and UV zones already on disk exactly as they were left
    (run_steady_state_scenario() itself further skips Phase 1 if
    steady_state_pipeline's own phase1_checkpoint.json is also present).
    """
    decision = _run_state["phase2_decision"]
    case_dir, room, settings = decision["case_dir"], decision["room"], decision["settings"]
    started_at, start = decision["started_at"], decision["start"]
    try:
        summary = _read_setup_summary(case_dir)
        _finish_steady_state(case_dir, room, settings, summary)
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
app.title = f"GUV-CFD v{APP_VERSION}"


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

_GRID_SNAP_NOTE = ("Position and size are automatically snapped OUTWARD to the mesh grid (cell "
                    "size, Settings menu) - the actual carved geometry may end up up to one cell "
                    "LARGER than entered here, but never smaller.")


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

        # ach/z-value are no longer edited here - the Run Simulations tab's
        # Z/ACH list fields (scenario-z-values/scenario-ach-values) are now
        # the only place to set them, since a single value is just a
        # length-1 list. Kept as hidden inputs (not a visible card) rather
        # than removed outright: every steady-state/decay call path still
        # reads settings["ach"]/settings["z-value"] directly for the
        # single-combination case, and a hidden component still satisfies
        # every existing Output/State reference to these ids - see
        # _sync_ach_z_from_run_tab, which keeps them in sync with the Run
        # tab's list fields whenever those resolve to exactly one value.
        html.Div([
            dcc.Input(id="ach", type="number", value=3.0),
            dcc.Input(id="z-value", type="number", value=2.0),
        ], style={"display": "none"}),

        _card("Inlet", _opening_controls("inlet", "xMin")
              + [_second_opening_controls("inlet2", "Inlet", "ceiling")]),

        _card("Outlet", _opening_controls("outlet", "xMax", is_inlet=False)
              + [_second_opening_controls("outlet2", "Outlet", "floor", is_inlet=False)]),

        _card("Mixing fan", [
            dbc.Checkbox(id="fan-enable", value=False, label="Enable fan", className="mb-2"),
            html.Div(id="fan-controls", children=[
                _labeled("Speed (m/s), 0.05–0.5 typical", dcc.Slider(
                    id="fan-speed", min=0.05, max=1.5, step=0.01, value=0.3,
                    marks={0.05: "0.05", 0.5: "0.5", 1.5: "1.5"},
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

        # Simulation type, decay solver timing, and every steady-state run-
        # budget field (phase1/2-iterations, T_ss window, deltaT scaling)
        # moved to the Run Simulations tab's "Simulation settings" modal -
        # this card only keeps what's genuinely room/problem geometry
        # (where the source is, how large its zone is), not run tuning.
        # "Target well-mixed steady-state T" was removed outright (not
        # moved) - it only sets the source strength G, and the system is
        # linear in G, so reduction_pct/eACH_uv (both ratios) and solver
        # convergence speed are completely independent of its value - see
        # steady_state_pipeline.REFERENCE_TARGET_T_SS.
        _card("Contaminant source geometry", [
            html.Div(_GRID_SNAP_NOTE, className="form-text small mb-2"),
            *_injection_position_controls(),
            _labeled("Source zone size (cells)", dcc.Input(
                id="source-zone-cells", type="number", value=1, min=1, max=20, step=1,
                className="form-control form-control-sm"),
                help_text="Side length of the cellZone the contaminant source injects into, in "
                          "MESH CELLS. In cells rather than metres because \"one cell\" is not one "
                          "number — blockMesh divides each room dimension into round(L/cell size) "
                          "cells, so a 2.57 m ceiling at a 0.1 m cell size builds 26 cells of "
                          "0.0988 m, and a metre value cannot mean one cell on every axis. The "
                          "total injection rate is unaffected by the size — it is divided by the "
                          "ACTUAL carved volume. Only used for steady-state runs."),
            _labeled("Breathing velocity (m/s)", dcc.Input(
                id="breathing-velocity", type="number", value=0.06, min=0, max=5, step=0.01,
                className="form-control form-control-sm"),
                help_text="Speed of the air the source blows, carrying the contaminant with it "
                          "instead of letting it appear in still air. 0.06 m/s is resting tidal "
                          "breathing. Set 0 for no airflow."),
            _labeled("Breathing direction X", dcc.Input(
                id="breathing-dir-x", type="number", value=0.0, step=0.1,
                className="form-control form-control-sm"),
                help_text="Direction the air is blown. Only the RATIO of X:Y:Z matters — the "
                          "vector is normalised automatically and the velocity above applied to "
                          "it. Defaults to (0,0,1), straight up. Aiming it at a vent "
                          "short-circuits contaminant into the extract and inflates the apparent "
                          "reduction — in one real case that lowered the steady-state "
                          "concentration by a factor of 2.7."),
            _labeled("Breathing direction Y", dcc.Input(
                id="breathing-dir-y", type="number", value=0.0, step=0.1,
                className="form-control form-control-sm"),
                help_text="See Breathing direction X."),
            _labeled("Breathing direction Z", dcc.Input(
                id="breathing-dir-z", type="number", value=1.0, step=0.1,
                className="form-control form-control-sm"),
                help_text="See Breathing direction X. (0,0,1) — the default — models an upward "
                          "plume; a horizontal vector models a directed exhale."),
        ]),

        # sim-type/pimple-end-time/pimple-write-interval/phase1-iterations/
        # phase2-iterations/t-ss-window-frac/suggest-duration-btn now render
        # for real inside the Run Simulations tab's "Simulation settings"
        # modal (see below) - not duplicated here. suggest-phases-btn is
        # hidden, not rebuilt there (redundant with deltaT scaling - see
        # compute_scaled_delta_t) - kept only so its existing callback
        # (_suggest_phases) still resolves.
        html.Div([
            dbc.Button("Suggest settling times (99.5%)", id="suggest-phases-btn"),
        ], style={"display": "none"}),

        # "Run simulation" is gone - the Run Simulations tab's "Start
        # simulations" button is now the only launch point (its Z/ACH list
        # fields, defaulting to a single value each, make a 1-combination
        # run and a sweep the same code path). "Continue to longer
        # duration" (decay-only) is hidden, not removed - its callbacks
        # (_start_continue/_continue_pipeline_thread) stay intact in case
        # it's needed again later. Both kept as hidden components (not
        # deleted) since several callbacks still reference their ids via
        # Output(..., allow_duplicate=True).
        html.Div([
            dbc.Button("Run simulation", id="run-btn"),
            dbc.Button("Continue to longer duration", id="continue-btn"),
            html.Div(id="run-validation-msg"),
        ], style={"display": "none"}),
    ], width=4, className="compact-panel", style={"maxHeight": "88vh", "overflowY": "auto"}),

    # --- right column: 3D preview ---
    dbc.Col([
        dcc.Graph(id="preview-graph", style={"height": "88vh"}, figure=_empty_preview_figure()),
    ], width=8),
])


def _checklist_item(step):
    return html.Li("☐ " + step, className="text-muted")


# Flow-convergence decision panel: shown only while _run_state["status"] ==
# "awaiting_decision" (see FlowConvergenceUndecided/_handle_flow_convergence_
# undecided) - a run that hit its iteration budget without a clear verdict
# stops HERE, on the Run Simulations tab where the user is already watching
# it, with the actual diagnostic numbers and three concrete next actions -
# never a dead-end error with no button to press. Hidden (style, toggled by
# _poll_run) rather than removed from the layout, so its own Input/Button
# ids always exist for Dash's callback graph regardless of current status.
flow_decision_panel = dbc.Alert(
    [
        html.Div("Flow convergence needs a decision", className="fw-bold mb-2"),
        html.Div(id="flow-decision-text", className="small mb-3", style={"whiteSpace": "pre-wrap"}),
        dbc.Row([
            dbc.Col(dcc.Input(id="flow-decision-iterations", type="number", min=100, step=100,
                               className="form-control form-control-sm"), width=3),
            dbc.Col(dbc.Button("Continue this many more iterations", id="flow-decision-continue-btn",
                                color="primary", size="sm", className="w-100"), width="auto"),
            dbc.Col(dbc.Button("Accept current state and proceed", id="flow-decision-accept-btn",
                                color="warning", size="sm", className="w-100"), width="auto"),
            dbc.Col(dbc.Button("Stop (leave as-is, decide later)", id="flow-decision-stop-btn",
                                color="secondary", size="sm", outline=True, className="w-100"), width="auto"),
        ], className="g-2 align-items-center"),
    ],
    id="flow-decision-panel", color="light", className="mb-3", style={"display": "none"},
)

# Phase2-resume panel: shown only while _run_state["status"] ==
# "awaiting_phase2_resume" (see case_awaiting_phase2_resume/_start_run) - a
# steady-state run whose setup already fully completed (mesh + flow
# convergence, possibly Phase 1 of the two-phase scenario too) but which
# never finished offers to pick up exactly where it left off, rather than
# a fresh Run silently regenerating the mesh and discarding that progress.
phase2_resume_panel = dbc.Alert(
    [
        html.Div("An unfinished steady-state run was found", className="fw-bold mb-2"),
        html.Div(id="phase2-resume-text", className="small mb-3", style={"whiteSpace": "pre-wrap"}),
        dbc.Row([
            dbc.Col(dbc.Button("Resume (skip completed steps)", id="phase2-resume-btn",
                                color="primary", size="sm", className="w-100"), width="auto"),
            dbc.Col(dbc.Button("Discard and start over", id="phase2-discard-btn",
                                color="secondary", size="sm", outline=True, className="w-100"), width="auto"),
        ], className="g-2 align-items-center"),
    ],
    id="phase2-resume-panel", color="light", className="mb-3", style={"display": "none"},
)

# Hidden legacy backing state for the single-run (1-combination) path's
# poller (_poll_run/run-poll) - no longer a visible tab (see the Run
# Simulations tab below, which is now the only place a run's progress is
# actually shown), but _poll_run's own Output/Input component graph still
# needs every one of these ids to exist somewhere in the layout, so this
# stays mounted, just invisible, rather than touching every one of
# _poll_run's (and its many callers') Outputs to remove them one by one -
# flow_decision_panel/phase2_resume_panel moved OUT of here into
# scenario_tab below (the only pieces of this a user still needs to see -
# the decision UX for a stuck flow-convergence/phase2 resume), so this is
# purely dead-weight bookkeeping the user never looks at.
_processing_legacy = html.Div([
    dbc.Row([
        dbc.Col([
            html.Div(id="run-status-text", className="fs-5 fw-semibold mb-2"),
            html.Div(id="run-elapsed", className="small text-muted"),
            html.Div(id="run-current-time", className="small text-muted mb-3"),
            dbc.Button("Stop", id="stop-btn", color="danger", size="sm", className="mb-4 me-2", disabled=True),
            dbc.Button("Pause", id="pause-btn", color="warning", size="sm", className="mb-4", disabled=True),
            html.Div("Steps", className="small fw-semibold text-uppercase mb-1"),
            html.Ul([_checklist_item(s) for s in DECAY_STEPS], id="run-checklist",
                    className="list-unstyled small"),
        ], width=4),
        dbc.Col([
            html.Div("Log", className="small fw-semibold text-uppercase mb-1"),
            html.Pre(id="run-log", className="small", style={
                "height": "72vh", "overflowY": "auto", "fontSize": "11px",
                "background": "rgba(127,127,127,0.08)", "padding": "8px",
                "border": "1px solid rgba(127,127,127,0.3)", "whiteSpace": "pre-wrap",
            }),
        ], width=8),
    ], className="mt-3"),
])

# Scenario Runs: sweep the currently-configured steady-state project over
# a comma-separated Z list x ACH list (full cross-product), one subfolder
# per combination directly under the project's case-dir - see
# scenario_runs.py. Every other Project Setup field (inlet/outlet/fan/
# monitoring/iterations) is reused unchanged from whatever's currently
# configured there; only z-value/ach vary per combination.
scenario_tab = html.Div([
    flow_decision_panel,
    phase2_resume_panel,
    dbc.Row([
    dbc.Col([
        html.Div(
            "Runs the current project's steady-state setup once per Z x ACH "
            "combination (every Z with every ACH), each into its own subfolder "
            "under the project directory. The flow field is converged once per "
            "distinct ACH and reused for every Z at that ACH, so a longer Z list "
            "at a fixed ACH is much cheaper than it looks. A single Z and a "
            "single ACH is just a 1-combination run - no separate mode to pick.",
            className="small text-muted mb-3",
        ),
        _labeled("Z values (comma-separated)",
                 dcc.Input(id="scenario-z-values", type="text", placeholder="e.g. 2, 6",
                           className="form-control form-control-sm")),
        _labeled("ACH values (comma-separated)",
                 dcc.Input(id="scenario-ach-values", type="text", placeholder="e.g. 3, 6",
                           className="form-control form-control-sm mt-2")),
        html.Div(id="scenario-combo-count", className="small text-muted mt-2 mb-3"),
        dbc.Button("Simulation settings…", id="scenario-settings-btn", color="secondary",
                   outline=True, className="w-100 mb-2"),
        dbc.Button("Start simulations", id="scenario-run-btn", color="success", className="w-100 mb-2"),
        dbc.Button("Stop simulation", id="scenario-stop-btn", color="danger", size="sm",
                    className="mb-2", disabled=True),
        dbc.Button("Extend / modify simulations...", id="extend-modal-open-btn", color="secondary",
                   outline=True, size="sm", className="w-100 mb-2"),
        html.Div(id="scenario-validation-msg", className="small text-danger mb-2"),
        html.Div(id="scenario-status-text", className="fs-6 fw-semibold mb-2"),
        dcc.Interval(id="scenario-poll", interval=2000, n_intervals=0, disabled=True),
    ], width=4),
    dbc.Col([
        html.Div("Simulation Progress", className="small fw-semibold text-uppercase mb-1"),
        html.Div(
            "Per-monitoring-point results stay under Analysis of Results - this table is "
            "room-average headline numbers only.",
            className="small text-muted mb-1",
        ),
        html.Div(id="scenario-progress-table"),
        html.Div("Running now", className="small fw-semibold text-uppercase mb-1 mt-3"),
        # One line per currently-active ACH/Z solve, overwritten in place
        # each poll (see app._scenario_status_update) rather than
        # appended - with several concurrent combinations each printing
        # their own "Time = N" line every step, letting those scroll in
        # the log below would flood it (see
        # scenario_runs._throttled_solver_callback's status_fn).
        html.Pre(id="scenario-live-status", className="small", style={
            "minHeight": "1.4em", "fontSize": "11px",
            "background": "rgba(127,127,127,0.08)", "padding": "6px 8px",
            "border": "1px solid rgba(127,127,127,0.3)", "whiteSpace": "pre-wrap",
        }),
        html.Div("Log", className="small fw-semibold text-uppercase mb-1 mt-3"),
        html.Pre(id="scenario-log", className="small", style={
            "height": "40vh", "overflowY": "auto", "fontSize": "11px",
            "background": "rgba(127,127,127,0.08)", "padding": "8px",
            "border": "1px solid rgba(127,127,127,0.3)", "whiteSpace": "pre-wrap",
        }),
    ], width=8),
    ], className="mt-3"),
])

# Simulation settings modal (Run Simulations tab): the fields that
# actually govern how a run behaves, moved out of Project Setup's old
# "Simulation type" card. Unlike settings_modal (Advanced Settings) below,
# every field here IS the real, live-bound project-setting component
# (sim-type, phase1-iterations, ... - same ids SETTINGS_FIELDS already
# lists) rather than a "settings-"-prefixed shadow copy - so most of this
# modal needs no Save/Cancel/Populate machinery at all: typing a value
# already updates the project's settings dict immediately, the same way
# ach/z-value always have, and it's written to the .guvcfd file the next
# time the project itself is saved. deltat-scaling-enabled/
# deltat-effective-fraction/deltat-target-fraction are the one place this
# differs subtly: they're ALSO project settings now (see
# steady_state_pipeline.merge_project_deltat_settings), but the Advanced
# Settings modal keeps its own separate "settings-deltat-*" copies as the
# fallback default for a brand-new project or one saved before these
# fields existed - editing one does not edit the other, by design (the
# user's own choice, accepting the small duplication for simplicity).
simulation_settings_modal = dbc.Modal(
    [
        dbc.ModalHeader(dbc.ModalTitle("Simulation settings")),
        dbc.ModalBody(
            [
                html.Div("Simulation type", className="small fw-bold text-uppercase mb-1"),
                dbc.RadioItems(
                    id="sim-type",
                    className="btn-group w-100 mb-3",
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
                    html.Div("Decay solver run settings", className="small fw-bold text-uppercase mb-1"),
                    _labeled("Suggested duration (s)", dbc.Row([
                        dbc.Col(dcc.Input(
                            id="pimple-end-time", type="number", value=120, min=10, max=7200, step=10,
                            className="form-control form-control-sm"), width=8),
                        dbc.Col(dbc.Button("Suggest", id="suggest-duration-btn", size="sm",
                                           color="secondary", outline=True, className="w-100"), width=4),
                    ], className="g-2"),
                        help_text="Starting value only - the actual run duration is now computed "
                                  "adaptively per the eACH/ACH fit-target settings (Settings menu) "
                                  "once the well-mixed eACH estimate is known, and overrides this."),
                    _labeled("Write interval (s)", dcc.Input(
                        id="pimple-write-interval", type="number", value=10, min=1, max=600, step=1,
                        className="form-control form-control-sm")),
                    dbc.Checkbox(id="mech-ach-only", value=False,
                                 label="Run mechanical ACH only (no UV)", className="mb-2 mt-2"),
                    html.Div(
                        "Skips the fluence/UV-inactivation pipeline entirely and measures just the "
                        "real, CFD-delivered ventilation air-change rate - for a pure ventilation "
                        "study, independent of whether the project has lamps. Needs ACH>0 (real "
                        "ventilation to measure).",
                        className="small text-muted mb-2",
                    ),
                    html.Hr(className="my-2"),
                ]),
                html.Div(id="steady-state-controls", children=[
                    html.Div("Steady-state run budget", className="small fw-bold text-uppercase mb-1"),
                    _labeled("Phase 1 iterations (no UV)", dcc.Input(
                        id="phase1-iterations", type="number", value=8000, min=500, max=50000, step=500,
                        className="form-control form-control-sm")),
                    _labeled("Phase 2 iterations (UV on)", dcc.Input(
                        id="phase2-iterations", type="number", value=3000, min=500, max=50000, step=500,
                        className="form-control form-control-sm")),
                    _labeled("T_ss moving-average window (fraction of samples)", dcc.Input(
                        id="t-ss-window-frac", type="number", value=0.15, min=0.01, max=0.9, step=0.01,
                        className="form-control form-control-sm"),
                        help_text="Room-wide T and every monitoring point report a trailing-window "
                                  "mean/CV over this fraction of the live per-iteration samples, "
                                  "instead of a single last-sample read. 0.15 = last 15% of samples."),
                    html.Hr(className="my-2"),
                    html.Div("Residence-time-scaled deltaT", className="small fw-bold text-uppercase mb-1"),
                    html.Div(
                        "Lets the iteration budget above cover enough real residence time to reach a "
                        "reliable steady state, by scaling OpenFOAM's own pseudo time step instead of "
                        "running more iterations - simpleFoam's U/p/k/omega solve has no time-derivative "
                        "term at all, so this doesn't touch flow-field stability.",
                        className="small text-muted mb-2",
                    ),
                    _settings_checkbox_field(
                        "deltat-scaling-enabled", "Scale time steps automatically",
                        "On by default. A case that already converges fine at a deltaT of 1 (typically "
                        "higher-ACH, short residence time) is completely unaffected.",
                        True,
                    ),
                    _settings_field(
                        "deltat-effective-fraction", "Expected ACH/eACH effectiveness",
                        "\"Effective\" = real, measured ventilation/UV removal typically runs below the "
                        "nominal value used to size this room. Lower-ACH rooms need a proportionally "
                        "larger time-step scale to reach the same real-time coverage within the "
                        "iteration budget above.",
                        "x", 0.7,
                    ),
                    _settings_field(
                        "deltat-target-fraction", "Target fraction of steady state",
                        "How close to true steady state the scaled deltaT targets within the iteration "
                        "budget above - 0.995 ~= 5.3 residence times.",
                        "", 0.995,
                    ),
                ]),
                dbc.Alert(
                    "These settings will be saved the next time the project saves. Everything above is "
                    "a project setting, same as Z/ACH - no separate file, no ambiguity about which value "
                    "was actually used for a given project's results.",
                    color="light", className="small mt-2 mb-0",
                ),
            ],
            style={"maxHeight": "64vh", "overflowY": "auto"},
        ),
        dbc.ModalFooter([
            dbc.Button("Close", id="simulation-settings-close-btn", color="primary"),
        ]),
    ],
    id="simulation-settings-modal", is_open=False, size="lg",
)


def _empty_analysis_figure():
    return go.Figure(layout=dict(
        annotations=[dict(text="Load a results.json to see analysis (or finish a run - it loads automatically)",
                           showarrow=False, font=dict(size=16, color="#888"))],
    ))


def _monitoring_summary_rows(monitoring):
    """Extra Analysis-tab rows for monitoring locations, if any were
    computed. Handles both decay's shape
    ({name: {t_seconds, volAverage_T, eACH_uv_assuming_well_mixed?}}) and
    steady-state's shape ({name: {phase1: {...}, phase2: {...}}}).
    """
    if not monitoring:
        return []
    rows = [("Monitoring locations", "")]
    for name, data in monitoring.items():
        if "phase1" in data:
            p1, p2 = data["phase1"], data["phase2"]
            T1, T2, reduction_pct, basis = point_reduction_basis(p1, p2)
            value = f"T_ss1={T1:.4g}, T_ss2={T2:.4g}" if T1 is not None and T2 is not None else "n/a"
            if basis == "extrapolated_T_infinity":
                value += " (extrapolated)"
            cv1, cv2 = p1.get("T_ss_cv"), p2.get("T_ss_cv")
            if cv1 is not None or cv2 is not None:
                cv1_text = f"{cv1 * 100:.1f}%" if cv1 is not None else "n/a"
                cv2_text = f"{cv2 * 100:.1f}%" if cv2 is not None else "n/a"
                value += f" (CV1={cv1_text}, CV2={cv2_text})"
            if reduction_pct is not None:
                value += f", reduction={reduction_pct:.1f}%"
        else:
            T_final = data["volAverage_T"][-1] if data["volAverage_T"] else None
            value = f"final volAverage(T)={T_final:.4g}" if T_final is not None else "n/a"
            if data.get("eACH_uv_assuming_well_mixed") is not None:
                value += f", eACH_uv={data['eACH_uv_assuming_well_mixed']:.4g}/hr"
        rows.append((f"  {name}", value))
    return rows


def _result_notes(result):
    """T-field explanation (always shown) plus a mixing-uniformity warning
    (only when monitoring points show the room isn't well mixed) - appended
    after the kv rows on both summary tabs.
    """
    notes = [T_FIELD_NOTE]
    if "phase1" in result and result.get("ventilation_ach_measured") is not None:
        notes.append(_effective_ach_note(result))
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
    reduction_pct = result.get("reduction_pct_corrected", result.get("reduction_pct"))
    rows += [
        ("Reduction", f"{reduction_pct:.1f}%{ach_note}"),
        (nominal_label, f"{result['eACH_uv_steady_state']:.4g} /hr{ach_note}"),
    ]
    if has_corrected:
        # Measured by a dedicated UV-off control run when one was part of
        # this sweep (see compute_corrected_eACH_uv_from_control's
        # docstring - more reliable than Phase 1's own point-source
        # buildup, which a small/localized source zone can leave under-
        # converged); falls back to the older Phase-1-derived method
        # otherwise (e.g. the single-run path, which doesn't run a
        # control) - ventilation_measurement_method tells us which.
        via_control = result.get("ventilation_measurement_method") == "control_run"
        ach_label = ("Effective ventilation ACH (measured, UV-off control run)" if via_control
                     else "Effective ventilation ACH (well-mixed-equivalent, from Phase 1)")
        rows.append((ach_label, f"{result['ventilation_ach_measured']:.4g} /hr{ach_note}"))
        rows.append(("eACH_uv, steady-state CFD-fit (measured ventilation ACH)",
                      f"{result['eACH_uv_steady_state_corrected']:.4g} /hr{ach_note}"))
    uv_efficiency_pct = combo_summary_metrics(result)["uv_efficiency_pct"]
    if uv_efficiency_pct is not None:
        rows.append(("Measured UV eff. %", f"{uv_efficiency_pct:.1f}%"))
    if result.get("mechanical_mixing_efficiency_pct") is not None:
        rows.append(("Mechanical mixing eff. %", f"{result['mechanical_mixing_efficiency_pct']:.1f}%"))
    cov1 = p1.get("spatial_cov")
    cov2 = p2.get("spatial_cov")
    if cov1 is not None:
        rows.append(("Spatial CoV, Phase 1 (mechanical mixing, no UV)", f"{cov1 * 100:.1f}%"))
    if cov2 is not None:
        rows.append(("Spatial CoV, Phase 2 (mechanical mixing, UV on)", f"{cov2 * 100:.1f}%"))
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
        ("eACH_uv, CFD-fit (nominal ventilation ACH)", f"{result['eACH_uv_assuming_well_mixed']:.4g} /hr"),
        ("Total ACH, effective", f"{result.get('total_ach_actual', 0):.3g} /hr"),
    ]
    if result.get("mixing_efficiency") is not None:
        rows.append(("Measured UV eff. %", f"{result['mixing_efficiency'] * 100:.1f}%"))
    if result.get("ventilation_ach_measured") is not None:
        rows.append(("Ventilation ACH (measured, UV-off control)",
                      f"{result['ventilation_ach_measured']:.4g} /hr"))
        rows.append(("eACH_uv, CFD-fit (measured ventilation ACH)",
                      f"{result['eACH_uv_actual']:.4g} /hr"))
        rows.append(("Measured UV eff. % (using measured ventilation ACH)",
                      f"{result['mixing_efficiency_actual'] * 100:.1f}%"))
    if result.get("mechanical_mixing_efficiency_pct") is not None:
        rows.append(("Mechanical mixing eff. %", f"{result['mechanical_mixing_efficiency_pct']:.1f}%"))
    if result.get("spatial_cov_final") is not None:
        rows.append(("Spatial CoV, final state (mechanical mixing)",
                      f"{result['spatial_cov_final'] * 100:.1f}%"))
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
                    "Budget of simpleFoam/pimpleFoam iterations spent trying to converge the flow "
                    "field before pausing to ask what to do next (continue further, or accept the "
                    "current state - see the Run Simulations tab if this happens). Lowering this does "
                    "NOT make a case fail faster and safely - it just means you'll be asked sooner, "
                    "possibly before the oscillation-acceptance check below even has enough evidence "
                    "to tell a stable oscillation from a genuinely still-growing one (it needs "
                    "2 x \"Oscillation check window\" chunks of history - if this budget runs out "
                    "before that, you won't get the automatic accept-if-bounded shortcut, only the "
                    "manual choice). Nothing is lost either way - progress is saved and resumable.",
                    "iterations", _adv_defaults["flow-max-iterations"],
                ),
                html.Hr(className="my-2"),
                html.Div("Flow oscillation acceptance", className="small fw-bold text-uppercase mb-1"),
                html.Div(
                    "Many room-ventilation flows (a jet or mixing fan impinging on a wall, floor, or "
                    "another jet) never truly satisfy the flow convergence tolerance above - they "
                    "settle into a genuinely oscillating, bounded pattern instead (real unsteady "
                    "turbulence, not a numerical problem). Rather than running forever (or up to the "
                    "max iterations above) chasing a residual that will never go flat, the flow field "
                    "gets ACCEPTED once its own oscillation stops growing or drifting - these two "
                    "settings control how that decision is made. Verified empirically that "
                    "downstream results (T_ss, eACH_uv) are insensitive to exactly which point in "
                    "the oscillation the field gets accepted at.",
                    className="small text-muted mb-2",
                ),
                _settings_field(
                    "settings-oscillation-window", "Oscillation check window",
                    "How many convergence-check chunks (each of size \"Flow convergence tolerance\"'s "
                    "own check interval) are compared against the same number of chunks before them "
                    "to decide whether the oscillation has stopped growing. Needs at least 2x this "
                    "many chunks of history before it can accept anything - raising it demands more "
                    "evidence (slower to accept, more confident when it does); lowering it accepts "
                    "sooner but on thinner evidence.",
                    "chunks", _adv_defaults["oscillation-window"],
                ),
                _settings_field(
                    "settings-oscillation-growth-tol", "Oscillation growth tolerance",
                    "How much the oscillation's amplitude (swing between its high and low points) is "
                    "allowed to grow in the most recent window compared to the window before it, and "
                    "still count as \"bounded\" rather than \"still getting worse.\" 1.5 means the "
                    "recent swing may be up to 50% larger than before and still be accepted. Lower = "
                    "stricter (only accepts a truly flat-amplitude oscillation); higher = looser "
                    "(tolerates a still-growing oscillation before giving up).",
                    "x", _adv_defaults["oscillation-growth-tol"],
                ),
                html.Hr(className="my-2"),
                html.Div("Ventilation delivery check", className="small fw-bold text-uppercase mb-1"),
                html.Div(
                    "Right after flow convergence, the actual volumetric flow rate leaving through "
                    "the outlet(s) is measured directly (summing the solved flux field) and compared "
                    "against the nominal ACH target - independent of whether the flow itself "
                    "converged or was only accepted via the oscillation check above. This exists "
                    "because a diffuser's velocity model can silently under- or over-deliver its "
                    "intended flow rate while every other check (flow residuals, T plateau) looks "
                    "completely normal - confirmed on a real case where a \"ceiling\" diffuser "
                    "delivered only ~38% of its nominal target despite a perfectly ordinary-looking "
                    "flow log. If this check fails, every downstream number (T_ss, eACH_uv, mixing "
                    "efficiency) is being computed against the wrong effective ACH - fix the inlet/"
                    "outlet geometry or diffuser type before trusting anything past this point.",
                    className="small text-muted mb-2",
                ),
                _settings_field(
                    "settings-ach-delivery-tol", "ACH delivery tolerance",
                    "How far the measured ventilation flow rate may differ from the nominal ACH "
                    "target before this is flagged as a setup problem rather than ordinary numerical "
                    "noise. Lower = stricter (flags smaller mismatches); higher = looser.",
                    "%", _adv_defaults["ach-delivery-tol"],
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
                _settings_field(
                    "settings-mass-balance-tol", "Mass balance cross-check tolerance (Phase 1)",
                    "Phase 1's contaminant source has a known injection rate G; at a true steady "
                    "state, whatever leaves through the outlet(s) must equal G exactly, so comparing "
                    "the trailing-window average of measured outlet removal to G is a convergence "
                    "signal that needs no curve-fitting assumptions at all (a single instantaneous "
                    "reading is NOT trustworthy for this - confirmed directly stable/low-noise "
                    "(~0.5% CV) only once properly windowed). Reported alongside the result as a "
                    "cross-check, not Phase 1's primary readiness gate (see the T∞ extrapolation "
                    "section below) - confirmed on a real run that reaching 95% mass balance needed "
                    "meaningfully more iterations than the cheaper extrapolation already trusted. "
                    "Lower = stricter; higher = looser. Not used for Phase 2 (UV also removes T via "
                    "the sink zones themselves, not just outflow, so the same simple identity "
                    "doesn't hold there).",
                    "%", _adv_defaults["mass-balance-tol"],
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
                    "iteration. 0.5 is the default, confirmed to fix a real case where 0.7 let "
                    "the flow field oscillate indefinitely instead of settling. Lower this number "
                    "a bit if oscillation is a problem; raising it can speed up convergence on "
                    "easy cases, at the cost of the same instability risk 0.7 used to hit.",
                    "", _adv_defaults["momentum-relaxation"],
                ),
                _settings_field(
                    "settings-scalar-relaxation", "Contaminant (T) relaxation",
                    "Damping factor for the transported contaminant field (T) each solver "
                    "iteration, independent of momentum/turbulence above — a stiff or strong "
                    "source/sink term can destabilize T even when the flow field itself is "
                    "perfectly well-behaved. Lower this first if a steady-state run's T grows "
                    "or oscillates without bound instead of settling toward equilibrium. Ignored "
                    "when adaptive T relaxation below is on.",
                    "", _adv_defaults["scalar-relaxation"],
                ),
                _settings_field(
                    "settings-decay-scalar-relaxation", "Contaminant (T) relaxation - decay mode",
                    "Under-relaxation for T in DECAY (transient) runs, kept separate from the steady-state value above. Leave at 1.0. Under-relaxation is a steady-state convergence device; in a transient run the time term already provides that stability, so relaxing here stabilises nothing - it only stops each timestep reaching the correct solution, so the UV sink is applied at a fraction of its strength every step. Measured: 0.05 understated eACH_uv by 14.7x.",
                    "", _adv_defaults["decay-scalar-relaxation"],
                ),
                _settings_checkbox_field(
                    "settings-adaptive-t-relaxation", "Use adaptive T relaxation",
                    "It has been found that for high Z*fluencerate values, significantly lower "
                    "T-relaxation values are needed to prevent crashing. An adaptive equation has "
                    "been established to adjust T relaxation automatically for non crashing and "
                    "shortest simulation times.",
                    _adv_defaults["adaptive-t-relaxation"],
                ),
                _settings_checkbox_field(
                    "settings-t-clamp-decay-enabled", "Use T divergence clamp",
                    "Independent of adaptive T relaxation above (that tunes how the solver "
                    "approaches a diverging cell; this catches the divergence itself if it "
                    "happens anyway). When on, any cell whose T strays outside [0, Tmax] each "
                    "iteration is replaced by a locally sink-decayed value instead of a hard "
                    "reset, keeping the correction physically motivated. Tmax is set per-run as "
                    "the multiplier below times Phase 1's own converged source-zone max T.",
                    _adv_defaults["t-clamp-decay-enabled"],
                ),
                _settings_field(
                    "settings-t-clamp-decay-multiplier", "T divergence clamp multiplier",
                    "Tmax = this value times Phase 1's own converged source-zone max T. Only "
                    "used when the T divergence clamp above is on.",
                    "x", _adv_defaults["t-clamp-decay-multiplier"],
                ),
                _settings_field(
                    "settings-phase1-tmax-multiplier", "Phase 1 clamp ceiling (x target T_ss)",
                    "Phase 1's own Tmax, as a multiple of the design target room average. The "
                    "ceiling is a DIVERGENCE BACKSTOP, not a peak shaver — the T<0 floor does the "
                    "real work and is always on. A source-zone peak around 6x the room average is "
                    "normal, so 10–20x leaves the ceiling clear of real physics while still "
                    "catching a cell running away toward 1e80. Phase 2 uses its own reference "
                    "(the multiplier above x Phase 1's converged source-zone max T).",
                    "x", _adv_defaults["phase1-tmax-multiplier"],
                ),
                _settings_checkbox_field(
                    "settings-decay-extend-to-target", "Extend decay runs until the target is met",
                    "Decay mode only. Run lengths are computed from an ASSUMED removal rate; when on, "
                    "each run is re-checked against the rate it actually achieved and continued if it "
                    "fell short. Without this a poorly-mixed room silently gets a run far too short to "
                    "measure and reports a negative eACH rather than an error.",
                    _adv_defaults["decay-extend-to-target"],
                ),
                _settings_field(
                    "settings-decay-max-total-time", "Decay run hard cap",
                    "Maximum simulated seconds for one decay run, extensions included. A run that still "
                    "hasn't met its target here is reported as capped and its figures flagged "
                    "unreliable, rather than quietly accepted.",
                    "s", _adv_defaults["decay-max-total-time"],
                ),
                html.Div(
                    "T is solved by its own scalarTransport function object, entirely outside "
                    "PIMPLE's/SIMPLE's own outer-iteration loop — the two settings below control "
                    "HOW MANY times per iteration it re-solves, and how tightly, before moving on. "
                    "Confirmed directly: relaxing T above without raising these lets the relaxation "
                    "bias the result instead of just damping it (a relaxed run converged to a "
                    "measured ventilation rate ~30% too low vs. an unrelaxed one) — see "
                    "\"OpenFOAM settings background.md\" at the repo root.",
                    className="small text-muted mb-2",
                ),
                _settings_field(
                    "settings-scalar-transport-ncorr", "Contaminant (T) outer correctors",
                    "How many times scalarTransport re-solves T per iteration before moving on "
                    "(0 = OpenFOAM's own default, exactly one pass regardless of relaxation). "
                    "Needs to be high enough, together with the tolerance below, for a relaxed T "
                    "to actually converge each iteration rather than just being damped once.",
                    "", _adv_defaults["scalar-transport-ncorr"],
                ),
                _settings_field(
                    "settings-scalar-transport-tolerance", "Contaminant (T) residual target",
                    "The initial-residual threshold scalarTransport checks each of its own "
                    "correction passes against (OpenFOAM's own default is 1 — essentially always "
                    "satisfied immediately, which is why raising outer correctors alone doesn't "
                    "help without tightening this too).",
                    "", _adv_defaults["scalar-transport-tolerance"],
                ),
                html.Hr(className="my-2"),
                html.Div("Phase 1 readiness (T∞ extrapolation)",
                          className="small fw-bold text-uppercase mb-1"),
                html.Div(
                    "By default, Phase 1 is accepted on the windowed-CV \"plateaued\" check alone, once "
                    "residence-time-scaled deltaT (see the Run Parameters / Advanced Settings deltaT "
                    "section) has sized the run to cover several real residence times - this reliably "
                    "reveals genuine ongoing drift via CV without needing a separate curve fit. T∞ "
                    "extrapolation below is still available for two, decoupled purposes: as a pure "
                    "speed optimization (stop a chunk early once T∞ itself has stabilized, regardless "
                    "of the CV-plateau verdict), and - only if the checkbox below is also turned on - "
                    "as an additional hard readiness GATE requiring several consecutive stable T∞ fits "
                    "before Phase 1 is accepted at all. That gate was the default before deltaT "
                    "scaling existed (a plateaued-looking CV=0.56% curve was once found still genuinely "
                    "rising, needing ~3x its iteration budget for mass balance to catch up), but "
                    "confirmed unreliable on real oscillating flow fields (never stabilizing across "
                    "resumes) and - critically - sweep-mode combinations have no resume UX for it at "
                    "all: enabling it there means one oscillating ACH value can silently fail an entire "
                    "sweep. Leave off unless you have a specific reason to distrust the CV-plateau check "
                    "for your case.",
                    className="small text-muted mb-2",
                ),
                _settings_checkbox_field(
                    "settings-t-infinity-early-stop-enabled", "Use T∞ extrapolation as a chunk early-stop",
                    "Off by default. A speed optimization only - lets a chunk end early once T∞ has "
                    "stabilized, without changing what counts as \"Phase 1 done\" (see the gate checkbox "
                    "below for that). Currently cannot be combined with \"Keep all time steps for "
                    "ParaView\" below - a real directory-naming bug was found in that combination "
                    "(since fixed, but blocked here as a precaution until it's proven out further).",
                    _adv_defaults["t-infinity-early-stop-enabled"],
                ),
                _settings_checkbox_field(
                    "settings-phase1-require-stable-extrapolation",
                    "Require a stable T∞ extrapolation before accepting Phase 1 (advanced)",
                    "Off by default - see the note above. When on, Phase 1 is NOT accepted until "
                    "several consecutive T∞ fits agree (the tolerance/streak settings below), even if "
                    "the CV-plateau check already looks fine; hitting the iteration ceiling without "
                    "that pauses the run for a decision (continue more / accept as-is / stop) on the "
                    "Run Simulations tab - single-run mode only, sweep mode has no resume path for this "
                    "and will simply fail the affected combination if this triggers there.",
                    _adv_defaults["phase1-require-stable-extrapolation"],
                ),
                _settings_field(
                    "settings-t-infinity-rel-tol", "T∞ stability tolerance",
                    "How much consecutive T∞ estimates (one chunk size apart - see below) may differ "
                    "from each other before Phase 1 counts as settled. Only takes effect when the "
                    "checkbox above is on.",
                    "%", _adv_defaults["t-infinity-rel-tol"],
                ),
                _settings_field(
                    "settings-phase-chunk-size", "Phase 1/2 chunk size",
                    "How many iterations Phase 1/2 run before re-checking T∞ stability and writing a "
                    "checkpoint. Shorter catches convergence earlier and loses less progress if the "
                    "run is interrupted (app restart, crash, reboot); longer has less per-chunk "
                    "overhead (a fresh solver launch, mesh re-read, postProcessing, field copy-back "
                    "every chunk) and a more stable T∞ refit.",
                    "iterations", _adv_defaults["phase-chunk-size"],
                ),
                _settings_field(
                    "settings-phase-write-interval", "Phase 1/2 write interval",
                    "How often (in iterations) Phase 1/2 write a snapshot within each chunk. Always "
                    "snapped down to the largest divisor of the chunk size above, so a snapshot lands "
                    "exactly at every chunk's end (never silently skipped).",
                    "iterations", _adv_defaults["phase-write-interval"],
                ),
                _settings_field(
                    "settings-phase1-extrapolation-streak", "Consecutive stable checks required",
                    "How many T∞ estimates in a row must agree (within the tolerance above) before "
                    "Phase 1 is accepted - a single agreeing pair could be coincidence; requiring "
                    "several in a row is a much less fragile signal. 3 is the validated default "
                    "(both test trajectories checked this session had already-tight fit quality by "
                    "the time they were first accepted).",
                    "checks", _adv_defaults["phase1-extrapolation-streak"],
                ),
                _settings_field(
                    "settings-phase1-t-initial", "Phase 1 starting T",
                    "T's initial value at the start of Phase 1. 0 (cold start) is the validated "
                    "default - confirmed directly that warm-starting at the target steady-state "
                    "value (the old default) needed ~40% MORE iterations to reach a stable, accurate "
                    "extrapolation on a real case, because a uniform starting guess doesn't match "
                    "the true (often highly non-uniform) steady spatial pattern, forcing an extra "
                    "redistribution transient on top of the real exponential build-up. A cold "
                    "start's curve is simpler and gets trusted sooner despite \"starting further "
                    "away.\"",
                    "", _adv_defaults["phase1-t-initial"],
                ),
                _settings_field(
                    "settings-phase1-settling-safety-multiplier", "Settling estimate safety margin",
                    "Multiplies the ACH-based theoretical settling-time estimate to get Phase 1's "
                    "starting iteration budget - confirmed directly that the plain theoretical "
                    "estimate ran short (a real case's fitted time constant came out ~3x the naive "
                    "theoretical value at the slowest tested ACH). Not a hard cap either way - the "
                    "extrapolation gate above can still pause and offer to extend further if even "
                    "this isn't enough (see the iteration ceiling below).",
                    "x", _adv_defaults["phase1-settling-safety-multiplier"],
                ),
                _settings_field(
                    "settings-phase1-max-iterations-ceiling", "Phase 1 iteration ceiling",
                    "Hard backstop for Phase 1's very first attempt - reached only if the "
                    "extrapolation gate above never stabilizes (a genuinely pathological case, or "
                    "the safety-margin estimate above being badly wrong for this project). Hitting "
                    "it pauses with a decision (continue more / accept as-is / stop) rather than "
                    "looping forever or silently accepting an unvalidated answer.",
                    "iterations", _adv_defaults["phase1-max-iterations-ceiling"],
                ),
                html.Hr(className="my-2"),
                html.Div("Residence-time-scaled deltaT (default mode)", className="small fw-bold text-uppercase mb-1"),
                html.Div(
                    "simpleFoam's own U/p/k/omega solve has no time-derivative term at all (pure "
                    "SIMPLE relaxation) - only T's own equation (a bolt-on function object) uses "
                    "OpenFOAM's pseudo-time step, and it's solved implicitly (unconditionally "
                    "stable), so scaling that step up lets a fixed, cheap iteration budget cover "
                    "more real residence time for low-ACH cases, at no extra compute cost and no "
                    "risk to the (already-converged, frozen) flow field. Confirmed directly on 3 "
                    "real cases (ACH=3/6/9): a 1500/1000-iteration run with this scaling matched a "
                    "4000/2500-iteration run at the historical deltaT=1 almost exactly. Purely "
                    "additive on top of the iteration budget/safety-margin settings above - a case "
                    "that already converges fine at deltaT=1 (typically higher-ACH) is unaffected.",
                    className="small text-muted mb-2",
                ),
                _settings_checkbox_field(
                    "settings-deltat-scaling-enabled", "Scale deltaT for low-ACH cases",
                    "On by default (recommended, the validated production default). Turn off to "
                    "fall back to the old deltaT=1/more-iterations-only behavior. Automatically "
                    "disabled whenever \"Keep all time steps for ParaView\" below is on - the two "
                    "aren't compatible (that feature's directory renaming assumes directory names "
                    "and iteration counts share the same units, which only holds at deltaT=1).",
                    _adv_defaults["deltat-scaling-enabled"],
                ),
                _settings_field(
                    "settings-deltat-effective-fraction", "Effective-rate derating factor",
                    "Measured ACH/eACH_uv typically runs below the nominal/well-mixed value used to "
                    "size this - the residence-time target is conservatively based on this fraction "
                    "of nominal ACH (Phase 1) or nominal ACH + well-mixed eACH_uv (Phase 2), not the "
                    "full nominal value, so the scaled deltaT doesn't undershoot a room that "
                    "ventilates/inactivates a bit worse than the idealized estimate.",
                    "x", _adv_defaults["deltat-effective-fraction"],
                ),
                _settings_field(
                    "settings-deltat-target-fraction", "Target fraction of steady state",
                    "How close to the true steady state the scaled deltaT targets reaching within "
                    "each phase's iteration budget - matches the settling-estimate safety margin's "
                    "own target above (0.995 = ~5.3 residence times, inside the \"4-6 residence "
                    "times\" criterion a well-mixed room's build-up needs - see \"OpenFOAM settings "
                    "background.md\").",
                    "fraction", _adv_defaults["deltat-target-fraction"],
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
                    "want to review the transient build-up/decay in ParaView afterward. Currently "
                    "cannot be combined with \"Enable T∞ early stopping\" above - see that "
                    "checkbox's note.",
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
                _settings_field(
                    "settings-max-co", "Decay solver Courant cap (maxCo)",
                    "Upper bound on the Courant number pimpleFoam's adaptive time step is allowed "
                    "to reach - higher lets the solver take bigger time steps (faster) but pushes "
                    "closer to the limit nOuterCorrectors=3 (fixed in the template) can still keep "
                    "stable/accurate each step. 5 is the original conservative default; a live "
                    "production sweep confirmed 10 stays stable with flat continuity error - see "
                    "ANALYSIS_LOG.md before pushing higher.",
                    "", _adv_defaults["max-co"],
                ),
                _settings_field(
                    "settings-max-concurrent-solves", "Max concurrent solves (sweeps)",
                    "How many OpenFOAM processes a sweep runs at once, any stage (flow, Phase 1, "
                    "control, Phase 2/decay). Was 9 (sized only for CPU-core headroom); lowered to 5 "
                    "after a real overnight sweep crashed itself in 5 separate waves from resource "
                    "contention, some with no trace in WSL's own kernel log - see 'Linux "
                    "installation.md'. Raise only if you've confirmed your machine has the RAM for "
                    "it; lower this if an unsupervised sweep keeps dying.",
                    "", _adv_defaults["max-concurrent-solves"],
                ),
                _settings_field(
                    "settings-decay-ach-min-fraction", "ACH fit target reduction",
                    "How far the UV-off control run (subfolder \"no_UV\", always run alongside the "
                    "main decay - see the removed \"no UV control\" toggle) decays before its "
                    "measured ventilation rate is trusted. Ventilation-only decay is often much "
                    "slower than UV+ventilation combined, so this has its own (lower) target rather "
                    "than sharing the eACH target below - pushing it to a much higher reduction can "
                    "cost hours for little gain (confirmed directly: one case needed ~9.5h of "
                    "simulated time to reach 99.9% at its measured 0.73/hr rate).",
                    "%", _adv_defaults["decay-ach-min-fraction"],
                ),
                _settings_field(
                    "settings-decay-each-min-fraction", "eACH fit target reduction (baseline)",
                    "The floor for how far the main UV-on run decays before its combined removal "
                    "rate is trusted - used whenever the higher target below would take too long "
                    "to be worth it (see that field).",
                    "%", _adv_defaults["decay-each-min-fraction"],
                ),
                _settings_field(
                    "settings-decay-each-max-fraction", "eACH fit target reduction (if cheap)",
                    "When the well-mixed eACH estimate is high enough that reaching this reduction "
                    "wouldn't exceed the same practical duration ceiling as the baseline target "
                    "above, the run targets this higher reduction instead - more data, for free, "
                    "whenever the combined decay rate is fast enough to make it cheap. Falls back "
                    "to the baseline target otherwise. Not pushed past this value even when very "
                    "cheap, to avoid needlessly long runs for no real accuracy gain.",
                    "%", _adv_defaults["decay-each-max-fraction"],
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
                html.Hr(className="my-2"),
                html.Div("Scenario sweep troubleshooting", className="small fw-bold text-uppercase mb-1"),
                html.Div(
                    "A scenario sweep (Run Simulations tab) shares one flow-converged base case, "
                    "Phase 1 run, and UV-off control run across every Z at the same ACH, then "
                    "deletes those shared directories once that ACH group's last Z finishes - "
                    "keeping a run's own working directory small, but also removing the one place "
                    "you could otherwise inspect the shared flow field/control run directly, or "
                    "reuse it to retry a single combination with different settings (e.g. a "
                    "different relaxation factor) without re-converging the flow from scratch. Turn "
                    "this on before a sweep you expect to need to troubleshoot afterward - off by "
                    "default, since a real multi-ACH sweep can leave several of these directories "
                    "(one flow base + one Phase 1 + one control run per ACH value) taking up real "
                    "disk space if never manually cleaned up.",
                    className="small text-muted mb-2",
                ),
                _settings_checkbox_field(
                    "settings-keep-shared-scratch-dirs", "Keep shared per-ACH scratch directories",
                    "Off by default so a sweep cleans up after itself. Turn on to keep "
                    "_base_ACH*/_phase1_ACH*/_control_ACH* directories on disk after the sweep "
                    "finishes, for inspection or reuse - you'll need to delete them yourself "
                    "afterward.",
                    _adv_defaults["keep-shared-scratch-dirs"],
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

grid_align_modal = dbc.Modal(
    [
        dbc.ModalHeader(dbc.ModalTitle("Openings don't align with the mesh grid")),
        dbc.ModalBody(id="grid-align-modal-body"),
        dbc.ModalFooter([
            dbc.Button("Keep as typed", id="grid-align-keep-btn", color="secondary", outline=True, size="sm"),
            dbc.Button("Apply suggested fix", id="grid-align-apply-btn", color="primary", size="sm"),
        ]),
    ],
    id="grid-align-modal", is_open=False,
)

# Sequential, one-conflict-at-a-time counterpart for inlet/outlet/2nd
# inlet/2nd outlet (see walk_opening_alignment_conflicts, _open_project) -
# grid_align_modal above stays as the bulk table+Apply/Keep dialog, now
# scoped to the contaminant source zone alone.
grid_align_seq_modal = dbc.Modal(
    [
        dbc.ModalHeader(dbc.ModalTitle("Mesh grid alignment")),
        dbc.ModalBody(id="grid-align-seq-modal-body"),
        dbc.ModalFooter([
            dbc.Button("Keep this value", id="grid-align-seq-decline-btn", color="secondary", outline=True, size="sm"),
            dbc.Button("Use suggested value", id="grid-align-seq-agree-btn", color="primary", size="sm"),
        ]),
    ],
    id="grid-align-seq-modal", is_open=False,
)

# --- "Extend / modify simulations..." modal (2026-08-10) ---
#
# Lets a user come back to an ALREADY-RUN project (possibly not the one
# currently open in Project Setup) and either apply a different UV design
# to the same room, extend/resume specific already-run combos, or sweep a
# few more Z/ACH combinations - all three consume the project_status.json
# fingerprinting work (see project_status.py) rather than blindly redoing
# everything. Same single-user, module-level pending-state pattern as
# _pending_grid_fix/_pending_alignment_walk/_pending_run - this is a local
# desktop tool, not a multi-tenant server.
_pending_extend_modal = {
    "project_dir": None, "project_name": None, "guv_path": None, "settings_path": None,
    "settings": None, "room": None, "status": None, "action": None,
}

# Every field ANY of the 3 action sub-forms could write into a launch call
# is declared statically (never generated dynamically via `children`) so
# Dash's callback graph can address them - only visibility toggles between
# the 3 sections, matching flow_decision_panel/phase2_resume_panel's own
# style-toggle pattern rather than _grid_align_seq_body's regenerate-the-
# whole-body pattern (that one has no user-editable inputs of its own to
# preserve across renders; these do).
_EXTEND_SECTION_IDS = ("extend-uv-section", "extend-extend-section", "extend-sweep-section")


def _ach_label_for_dirs(ach):
    """Folder-name-safe ACH label - matches scenario_runs._ach_label /
    project_status._ach_label exactly, duplicated here rather than
    imported (same reasoning those two already keep separate copies for:
    a tiny, stable formatting rule, not worth cross-module coupling).
    """
    return "sealed" if ach <= 0 else f"{ach:g}"


def _extend_status_table(status, project_dir=None):
    """dbc.Table (or an explanatory message) for a loaded project_status
    dict's combos - Z, ACH, status, which .guv design produced it and
    which simulation mode it ran under (see compute_guv_design_suffix/
    compute_sim_type_suffix - two combos can share the same Z/ACH but
    differ in design and/or mode, so without these columns they'd look
    identical), a simple checkmark for whether each fingerprint is on
    record (a human doesn't need to see the raw hash - see
    project_status.py's own combo schema), and whether this combo's ACH
    group still has a Control run / Phase 1 (steady-state) working copy
    sitting on disk. Started/finished timestamps are deliberately
    NOT shown - they're only ever set by a live run, so a status file
    rebuilt from disk (see rebuild_project_status_from_disk, the common
    case for a project predating this feature) never has them, making
    those columns blank far more often than not.

    Control/Phase 1 are checked by plain folder existence under
    project_dir (_control_ACH<label>/_phase1_ACH<label>), the same
    informational-only spirit as the Flow/UV columns - NOT a promise
    those are still valid for the CURRENT project settings (see
    find_reusable_ach_base for the fingerprint-validated version of that
    question, used when actually deciding whether to reuse them).
    project_dir=None (nothing loaded yet) just shows both as blank.

    Empty combos (a project this feature has never touched, or a brand
    new one) is a real, expected state, not an error - shown plainly.
    """
    combos = (status or {}).get("combos") or {}
    if not combos:
        return html.P(
            "No recorded runs for this folder yet. If it's a brand new project directory, the "
            "3 actions below still work, there's just nothing to show a history of. If it "
            "already has real runs in it from before this feature existed, use \"Create a "
            "status file from the runs already in this folder\" below to rebuild one from them.",
            className="small text-muted mb-0",
        )
    rows = []
    for key in sorted(combos, key=lambda k: (combos[k].get("ach", 0), combos[k].get("z", 0))):
        c = combos[key]
        ach = c.get("ach")
        control_ok = phase1_ok = False
        if project_dir is not None and ach is not None:
            label = _ach_label_for_dirs(ach)
            control_ok = Path(f"{project_dir}/_control_ACH{label}").exists()
            phase1_ok = Path(f"{project_dir}/_phase1_ACH{label}").exists()
        design = Path(c["guv_path"]).stem if c.get("guv_path") else ""
        mode = c.get("sim_type", "")
        rows.append(html.Tr([
            html.Td(f"{c.get('z')}"), html.Td(f"{c.get('ach')}"), html.Td(c.get("status", "")),
            html.Td(design), html.Td(mode),
            html.Td("✓" if c.get("flow_fingerprint") else ""),
            html.Td("✓" if c.get("uv_fingerprint") else ""),
            html.Td("✓" if control_ok else ""),
            html.Td("✓" if phase1_ok else ""),
        ]))
    return dbc.Table(
        [html.Thead(html.Tr([html.Th(h) for h in
                              ("Z", "ACH", "Status", "Design", "Mode", "Flow", "UV", "Control (ACH)", "Phase 1")]))]
        + [html.Tbody(rows)],
        bordered=True, size="sm", className="mb-0",
    )


def _extend_modal_body(status, project_dir=None, message=None):
    return [
        html.Div(message, className="small text-danger mb-2") if message else None,
        _extend_status_table(status, project_dir),
    ]


extend_modal = dbc.Modal(
    [
        dbc.ModalHeader(dbc.ModalTitle("Extend / modify existing simulations")),
        dbc.ModalBody([
            dbc.Row([
                dbc.Col(dcc.Input(id="extend-project-dir-input", type="text", placeholder="Project folder...",
                                   className="form-control form-control-sm"), width=8),
                dbc.Col(dbc.Button("Browse...", id="extend-browse-btn", size="sm", outline=True,
                                    color="secondary", className="w-100"), width=2),
                dbc.Col(dbc.Button("Load", id="extend-load-btn", size="sm", color="primary",
                                    className="w-100"), width=2),
            ], className="g-2 mb-3"),
            html.Div(id="extend-status-table"),
            html.Div(
                dbc.Button("Create a status file from the runs already in this folder",
                           id="extend-rebuild-status-btn", size="sm", outline=True, color="info"),
                id="extend-rebuild-status-wrapper", style={"display": "none"}, className="mt-2",
            ),
            html.Div([
                html.P(
                    "Every ACH group's flow field/UV-off control run is kept in a temporary "
                    "folder here (named _base_ACH<value> / _control_ACH<value> / _phase1_ACH<value>) "
                    "so a later sweep in this same folder can reuse it instead of re-meshing. "
                    "These are safe to delete any time - they hold no results of their own, "
                    "only reusable working copies; your actual Z/ACH simulation results are never "
                    "touched by this.",
                    className="small text-muted mb-1",
                ),
                dbc.Button("Delete temporary flow-field folders...", id="extend-cleanup-btn",
                           size="sm", outline=True, color="danger"),
            ], className="mt-3 mb-2"),
            html.Hr(),
            dbc.ButtonGroup([
                dbc.Button("Apply different UV design to same room", id="extend-action-uv-btn",
                           size="sm", outline=True, color="secondary"),
                dbc.Button("Extend specific run(s)", id="extend-action-extend-btn",
                           size="sm", outline=True, color="secondary"),
                dbc.Button("Add more Z/ACH sweeps", id="extend-action-sweep-btn",
                           size="sm", outline=True, color="secondary"),
            ], className="mb-3 d-flex"),

            # --- "Apply different UV design" sub-form ---
            html.Div([
                dbc.Row([
                    dbc.Col(dcc.Input(id="extend-uv-guv-path", type="text", placeholder=".guv file...",
                                       className="form-control form-control-sm"), width=8),
                    dbc.Col(dbc.Button("Browse...", id="extend-uv-browse-btn", size="sm", outline=True,
                                        color="secondary", className="w-100"), width=4),
                ], className="g-2 mb-2"),
                _labeled("Z values (comma-separated)", dcc.Input(id="extend-uv-z-values", type="text",
                                                                    placeholder="e.g. 2, 6",
                                                                    className="form-control form-control-sm")),
                html.P("Prefilled with this project's own Z values - edit the list to re-evaluate "
                       "different ones under the new design instead.",
                       className="small text-muted mt-1 mb-0"),
                html.P("Every ACH already swept in this project will be re-evaluated under the new "
                       "design - any whose flow settings haven't changed reuse the existing flow "
                       "field/control run instead of re-solving them.",
                       className="small text-muted mt-2 mb-0"),
                # Only shown for a steady-state project - switching TO decay
                # mode is supported (the shared flow base and UV-off
                # control run are identical either way, and decay mode has
                # no Phase 1 concept at all, so only each Z's own UV-on
                # decay solve is new work); the reverse (decay -> steady-
                # state) isn't offered.
                html.Div([
                    _labeled("Simulation mode", dcc.Dropdown(
                        id="extend-uv-sim-type", clearable=False, value="steady_state",
                        options=[{"label": "Steady state (same as this project)", "value": "steady_state"},
                                 {"label": "Decay mode", "value": "decay"}])),
                    html.P("Switching to Decay mode reuses this ACH's already-converged flow field and "
                           "UV-off control run - only each Z's own UV-on decay solve is new work.",
                           className="small text-muted mt-1 mb-0"),
                ], id="extend-uv-mode-row", style={"display": "none"}),
            ], id="extend-uv-section", style={"display": "none"}),

            # --- "Extend specific run(s)" sub-form ---
            html.Div([
                _labeled("Combination(s) to extend", dcc.Dropdown(id="extend-combo-dropdown", multi=True)),
                _labeled("New end time (s) - decay only in this version", dcc.Input(
                    id="extend-duration-input", type="number", className="form-control form-control-sm")),
                html.P("Steady-state combinations aren't extendable from this modal yet - use the "
                       "per-run Resume panel (Run Simulations tab) for those.",
                       className="small text-muted mt-2 mb-0"),
            ], id="extend-extend-section", style={"display": "none"}),

            # --- "Add more Z/ACH sweeps" sub-form ---
            html.Div([
                _labeled("Z values (comma-separated)", dcc.Input(
                    id="extend-sweep-z-values", type="text", className="form-control form-control-sm")),
                _labeled("ACH values (comma-separated)", dcc.Input(
                    id="extend-sweep-ach-values", type="text", className="form-control form-control-sm mt-2")),
                html.P("Already-completed combinations are skipped automatically.",
                       className="small text-muted mt-2 mb-0"),
            ], id="extend-sweep-section", style={"display": "none"}),

            html.Div(id="extend-action-msg", className="small text-danger mt-2"),
        ]),
        dbc.ModalFooter([
            dbc.Button("Cancel", id="extend-cancel-btn", color="secondary", outline=True, size="sm"),
            dbc.Button("Run new Sweep(s)", id="extend-run-btn", color="success", size="sm"),
        ]),
    ],
    id="extend-modal", is_open=False, size="lg", scrollable=True,
)


app.layout = dbc.Container([
    dcc.Store(id="fresh-room-load"),
    dcc.Store(id="results-data"),
    dcc.Store(id="results-case-dir"),
    dcc.Interval(id="run-poll", interval=2000, n_intervals=0, disabled=True),
    # Fires ONCE, shortly after every page load (including a plain browser
    # refresh) - run-poll/scenario-poll's own "disabled" state lives only in
    # the browser (a dcc.Interval prop), always reset back to its layout
    # default (True) on load, with nothing to re-enable it from the SERVER's
    # actual _run_state/_scenario_state (a run in progress is tracked
    # entirely server-side, in a plain module-level dict, completely
    # independent of any browser session - confirmed directly: a live decay
    # run's pimpleFoam processes kept computing normally through a page
    # refresh that left the UI showing nothing). See _resync_pollers below.
    dcc.Interval(id="resync-check", interval=500, n_intervals=0, max_intervals=1),
    dcc.ConfirmDialog(id="overwrite-confirm"),
    dcc.ConfirmDialog(id="extend-cleanup-confirm"),
    dbc.Modal(
        [
            dbc.ModalHeader(dbc.ModalTitle(id="help-modal-title")),
            dbc.ModalBody(dcc.Markdown(id="help-modal-body", link_target="_blank")),
        ],
        id="help-modal", is_open=False, size="lg", scrollable=True,
    ),
    settings_modal,
    simulation_settings_modal,
    grid_align_modal,
    grid_align_seq_modal,
    extend_modal,
    dbc.Row(
        dbc.Col(html.H4([
            "GUV-CFD ",
            html.Small(f"v{APP_VERSION}", className="text-muted"),
        ], className="mt-3 mb-1"), width="auto"),
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
        dbc.Tab(scenario_tab, label="Run Simulations", tab_id="scenario-runs"),
        dbc.Tab(analysis_tab, label="Analysis of Results", tab_id="analysis"),
    ], active_tab="project-setup", className="mb-3", id="main-tabs"),
    html.Div(_processing_legacy, style={"display": "none"}),
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
            data = migrate_result_keys(json.load(f))  # pre-2026-09-02 files use the old key names
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
    settings = _collect_settings(values)
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

    # Backfills any OpenFOAM/meshing/solver setting (maxCo, cell size,
    # zone-binning count, relaxation factors, tolerances, iteration
    # budgets, ...) missing from this project's .guvcfd - see
    # app_settings.capture_openfoam_settings's docstring. A key already
    # present (whether auto-captured on an earlier save or hand-edited) is
    # never overwritten - that's what makes a project's settings pinned
    # and reproducible regardless of what the *global* advanced settings
    # later change to.
    capture_openfoam_settings(settings, load_advanced_settings())
    with open(path, "w") as f:
        json.dump(settings, f, indent=2)
    _loaded["settings_path"] = path
    return path.replace("\\", "/").rsplit("/", 1)[-1]


_open_outputs = [
    Output("project-name-display", "children", allow_duplicate=True),
    Output("project-status", "children", allow_duplicate=True),
    Output("grid-align-modal", "is_open", allow_duplicate=True),
    Output("grid-align-modal-body", "children", allow_duplicate=True),
    Output("grid-align-seq-modal", "is_open", allow_duplicate=True),
    Output("grid-align-seq-modal-body", "children", allow_duplicate=True),
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
    "settings-flow-rel-tol", "settings-flow-max-iterations",
    "settings-oscillation-window", "settings-oscillation-growth-tol", "settings-ach-delivery-tol",
    "settings-plateau-rel-tol", "settings-mass-balance-tol",
    "settings-momentum-relaxation", "settings-scalar-relaxation",
    "settings-decay-scalar-relaxation", "settings-adaptive-t-relaxation",
    "settings-t-clamp-decay-enabled", "settings-t-clamp-decay-multiplier",
    "settings-phase1-tmax-multiplier",
    "settings-scalar-transport-ncorr", "settings-scalar-transport-tolerance",
    "settings-t-infinity-early-stop-enabled", "settings-phase1-require-stable-extrapolation",
    "settings-t-infinity-rel-tol",
    "settings-phase-chunk-size", "settings-phase-write-interval",
    "settings-phase1-t-initial", "settings-phase1-extrapolation-streak",
    "settings-phase1-settling-safety-multiplier", "settings-phase1-max-iterations-ceiling",
    "settings-deltat-scaling-enabled", "settings-deltat-effective-fraction", "settings-deltat-target-fraction",
    "settings-keep-all-timesteps",
    "settings-pimple-delta-t", "settings-max-co", "settings-max-concurrent-solves",
    "settings-decay-ach-min-fraction", "settings-decay-each-min-fraction", "settings-decay-each-max-fraction",
    "settings-decay-extend-to-target", "settings-decay-max-total-time",
    "settings-mesh-cell-size",
    "settings-uv-zone-bins",
    "settings-keep-shared-scratch-dirs",
]
# Same order as _SETTINGS_FIELD_IDS - maps each GUI field to its
# app_settings.py storage key (see ADVANCED_SETTINGS_DEFAULTS).
_SETTINGS_FIELD_KEYS = [
    "flow-rel-tol", "flow-max-iterations",
    "oscillation-window", "oscillation-growth-tol", "ach-delivery-tol",
    "plateau-rel-tol", "mass-balance-tol",
    "momentum-relaxation", "scalar-relaxation", "decay-scalar-relaxation",
    "adaptive-t-relaxation",
    "t-clamp-decay-enabled", "t-clamp-decay-multiplier", "phase1-tmax-multiplier",
    "scalar-transport-ncorr", "scalar-transport-tolerance",
    "t-infinity-early-stop-enabled", "phase1-require-stable-extrapolation",
    "t-infinity-rel-tol",
    "phase-chunk-size", "phase-write-interval",
    "phase1-t-initial", "phase1-extrapolation-streak",
    "phase1-settling-safety-multiplier", "phase1-max-iterations-ceiling",
    "deltat-scaling-enabled", "deltat-effective-fraction", "deltat-target-fraction",
    "keep-all-timesteps",
    "pimple-delta-t", "max-co", "max-concurrent-solves",
    "decay-ach-min-fraction", "decay-each-min-fraction", "decay-each-max-fraction",
    "decay-extend-to-target", "decay-max-total-time",
    "mesh-cell-size", "uv-zone-bins",
    "keep-shared-scratch-dirs",
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
    Output("simulation-settings-modal", "is_open"),
    Input("scenario-settings-btn", "n_clicks"),
    Input("simulation-settings-close-btn", "n_clicks"),
    State("simulation-settings-modal", "is_open"),
    prevent_initial_call=True,
)
def _toggle_simulation_settings_modal(_open, _close, is_open):
    # No populate/save round-trip needed (unlike settings-modal above) -
    # every field in this modal is either a live-bound project setting
    # (already up to date, saves with the project) or one of the deltat-*
    # fields, which read their initial value straight from the layout
    # default/whatever the loaded project already set via SETTINGS_FIELDS -
    # opening/closing this modal never needs to re-fetch or write anything
    # itself.
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
    # Defense-in-depth, kept even after fixing the actual bug this guarded
    # against (steady_state_pipeline._rename_chunk_time_dirs used to rename
    # every numbered directory on disk, not just the current chunk's own -
    # confirmed corrupting a real case directory when both these were on
    # together). The two features are independent, and now-fixed, so this
    # block is deliberately conservative rather than load-bearing - remove
    # it once the fix has enough runs behind it to trust the combination.
    if settings.get("t-infinity-early-stop-enabled") and settings.get("keep-all-timesteps"):
        return ('Not saved - "Enable T∞ early stopping" and "Keep all time steps for ParaView" '
                "can't both be on at once (a directory-naming bug was found in this combination; "
                "it's since been fixed, but this block is left in as a precaution for now). "
                "Turn one off before saving.")
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
    # source-zone-size used to be a global advanced setting, not saved per-
    # project - this is that old global default, for any .guvcfd saved
    # before it moved to a per-project field.
    "source-zone-cells": 1,
    "breathing-velocity": 0.06,
    "breathing-dir-x": 0.0, "breathing-dir-y": 0.0, "breathing-dir-z": 1.0,
    # deltaT scaling used to be a global advanced setting too - same
    # defaults as ADVANCED_SETTINGS_DEFAULTS at the time it moved to a
    # per-project field, for the same backward-compat reason.
    "deltat-scaling-enabled": True, "deltat-effective-fraction": 0.7, "deltat-target-fraction": 0.995,
}


# Maps check_settings_grid_alignment's "name" back to the actual form
# field. Inlet/Outlet/2nd inlet/2nd outlet moved to the new sequential
# walk_opening_alignment_conflicts flow (see _open_project,
# walk_opening_alignment_conflicts's own docstring for why they get a
# richer size-then-center treatment) - this bulk table+Apply/Keep modal
# now only ever covers the contaminant source zone, a single symmetric
# cube with no width/height/position-pair/wall-center-bias distinction
# for the new per-opening flow to add value to.
_GRID_ALIGN_FIELD_IDS = {
    "Contaminant source position": ("inject-x-input", "inject-y-input", "inject-z-input"),
}
_GRID_ALIGN_ALL_FIELD_IDS = [fid for fids in _GRID_ALIGN_FIELD_IDS.values() for fid in fids]

# Holds the mismatch list between _open_project detecting it and the
# modal's Apply/Keep button click (two separate callbacks/requests) -
# single-user local tool, same pattern as _pending_run.
_pending_grid_fix = {"mismatches": None}

# Holds the live walk_opening_alignment_conflicts() generator between
# _open_project priming it and each Agree/Decline button click advancing
# it - same single-user, module-level pending-state pattern as
# _pending_grid_fix/_pending_run above (this is a local desktop tool, not
# a multi-tenant server, so a live, non-serializable generator object
# living here between callback round-trips is consistent with the
# existing architecture, not a new class of risk).
#   walker: the live generator, or None when idle.
#   current: the conflict dict currently shown in the modal, so the
#       Agree/Decline callback knows exactly which field to write without
#       recomputing anything.
#   any_applied: whether at least one Agree happened during this walk -
#       the "you must save" reminder should only appear if something
#       actually changed (a walk can end via a mix of some-agreed/some-
#       declined steps, unlike the old bulk modal's two fully separate
#       terminal actions, so this needs to be tracked explicitly).
_pending_alignment_walk = {"walker": None, "current": None, "any_applied": False}


def _check_grid_alignment(settings, room):
    """Mismatches for this project's contaminant source POSITION against
    the current mesh grid (see run_pipeline.check_settings_grid_alignment's
    docstring for the real bug this catches early) - empty list if
    already grid-aligned, or if the check itself can't run (e.g. a
    hand-edited project file missing a core field - not worth failing the
    whole "Open" action over a diagnostic check). Inlet/outlet/2nd-inlet/
    2nd-outlet are NOT included here anymore - see
    walk_opening_alignment_conflicts/_open_project for their own,
    separate sequential flow.
    """
    try:
        adv = merge_project_openfoam_settings(settings, load_advanced_settings())
        mismatches = check_settings_grid_alignment(
            settings, room, adv["mesh-cell-size"])
        return [m for m in mismatches if m["name"] == "Contaminant source position"]
    except Exception:
        return []


def _fmt_size(size):
    return "x".join(f"{v:.3g}" for v in size) + "m"


def _grid_align_seq_body(conflict):
    """dbc.ModalBody children for ONE conflict from
    walk_opening_alignment_conflicts - unlike _grid_align_modal_body
    (a bulk table of every mismatch at once), this shows exactly one
    opening/axis at a time, with an explanation tailored to whether it's
    a size or a center conflict (see walk_opening_alignment_conflicts's
    own docstring for why those are two different, sequential kinds of
    fix).
    """
    axis_label = conflict["axis_label"]
    if conflict["kind"] == "size":
        why = (f"{conflict['current']:.3g} m isn't a multiple of the mesh cell size, "
               f"so the built opening would end up bigger than what you typed.")
    else:
        why = ("At the corrected size, this position won't land the opening's edges on mesh "
               "cell boundaries. Shifting the center slightly fixes this without changing the "
               "size any further.")
    return [
        html.H6(f"{conflict['opening']} — {axis_label}"),
        html.P(why, className="small text-muted"),
        dbc.Table(
            html.Tbody([
                html.Tr([html.Td("Current"), html.Td(f"{conflict['current']:.4g} m")]),
                html.Tr([html.Td("Suggested"), html.Td(f"{conflict['suggested']:.4g} m")]),
            ]),
            size="sm", borderless=True, className="mb-0",
        ),
    ]


def _grid_align_modal_body(mismatches):
    """dbc.ModalBody children explaining the mesh-snap mismatch: why it
    happens, what actually happens if it's left alone (a record-accuracy
    issue, NOT a simulation-safety one - the mesh generator always snaps
    outward and safely regardless of this dialog's outcome), and exactly
    which fields would change to what.
    """
    rows = [
        html.Tr([html.Td(m["name"]), html.Td(_fmt_size(m["nominal"])), html.Td("→"), html.Td(_fmt_size(m["actual"]))])
        for m in mismatches
    ]
    return [
        html.P(
            "This project's mesh cell size doesn't evenly divide one or more opening/source-zone "
            "positions and sizes below. When the mesh is actually built, each affected edge gets "
            "snapped outward to the nearest cell boundary (never shrunk - only ever grown, by at "
            "most one cell) so the carved geometry is always at least as large as requested. That "
            "snapping happens automatically and safely either way - this dialog is about keeping "
            "this project's own recorded numbers honest, not about simulation correctness."
        ),
        html.P(
            [
                html.B("If you don't apply this: "),
                "the values shown here and saved in this project file will keep reading the "
                "original numbers you typed, even though the actual simulated opening/source "
                "zone will be the larger, snapped size instead. The simulation itself is still "
                "correct - but this project's settings, this report, and anyone comparing across "
                "projects later would see a number that doesn't match what was actually built.",
            ]
        ),
        html.P(html.B("Suggested changes:")),
        dbc.Table(
            [html.Thead(html.Tr([html.Th("Field"), html.Th("Typed"), html.Th(""), html.Th("Will actually be")]))]
            + [html.Tbody(rows)],
            bordered=True, size="sm", className="mb-2",
        ),
        html.P("Apply these to this project's own fields now?", className="mb-0"),
    ]


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
    modal_open, modal_body = False, dash.no_update
    seq_modal_open, seq_modal_body = False, dash.no_update
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

            # Contaminant source zone: stash the mismatch (if any) now, but
            # don't open its modal yet if the inlet/outlet sequential walk
            # below is about to open its own modal - only one modal should
            # be visible at a time; see _open_bulk_modal_after_seq_modal_closes
            # for what opens it once that walk finishes.
            mismatches = _check_grid_alignment(settings, room)
            if mismatches:
                _pending_grid_fix["mismatches"] = mismatches

            # Inlet/outlet/2nd-inlet/2nd-outlet: new sequential, one-
            # conflict-at-a-time walk (see walk_opening_alignment_conflicts).
            adv = merge_project_openfoam_settings(settings, load_advanced_settings())
            walker = walk_opening_alignment_conflicts(settings, room, adv["mesh-cell-size"])
            first_conflict = next(walker, None)
            if first_conflict is not None:
                _pending_alignment_walk["walker"] = walker
                _pending_alignment_walk["current"] = first_conflict
                _pending_alignment_walk["any_applied"] = False
                seq_modal_open, seq_modal_body = True, _grid_align_seq_body(first_conflict)
            elif mismatches:
                modal_open, modal_body = True, _grid_align_modal_body(mismatches)
        except Exception as e:
            status = f"Failed to reload {guv_path}: {e}"

    _loaded["settings_path"] = path
    proj_name = path.replace("\\", "/").rsplit("/", 1)[-1]

    # scenario-z-values/scenario-ach-values are the Run Simulations tab's
    # own Z/ACH list fields (a project's real ach/z-value are still a
    # single scalar each internally) - for a project saved before these
    # list fields existed, fall back to that scalar's own string
    # representation instead of a static default, so the Run tab starts
    # pre-filled with "the project's ACH/Z" rather than blank.
    def _field_value(fid):
        if fid == "scenario-z-values" and fid not in settings:
            return str(settings.get("z-value", _NEW_FIELD_DEFAULTS.get("z-value", "")))
        if fid == "scenario-ach-values" and fid not in settings:
            return str(settings.get("ach", _NEW_FIELD_DEFAULTS.get("ach", "")))
        return settings.get(fid, _NEW_FIELD_DEFAULTS.get(fid))

    field_values = [_field_value(fid) for fid in SETTINGS_FIELDS]
    max_values = []
    for _prefix, _label, dim, _default_fn, *_rest in POSITION_FIELDS:
        if room is not None:
            dim_size = round(getattr(room, dim), 3)
            max_values += [dim_size, dim_size]
        else:
            max_values += [dash.no_update, dash.no_update]

    return tuple([proj_name, status, modal_open, modal_body, seq_modal_open, seq_modal_body]
                 + field_values + max_values)


@app.callback(
    Output("grid-align-modal", "is_open", allow_duplicate=True),
    Output("project-status", "children", allow_duplicate=True),
    [Output(fid, "value", allow_duplicate=True) for fid in _GRID_ALIGN_ALL_FIELD_IDS],
    Input("grid-align-apply-btn", "n_clicks"),
    State("project-status", "children"),
    prevent_initial_call=True,
)
def _apply_grid_align_fix(n_clicks, current_status):
    mismatches = _pending_grid_fix.get("mismatches") or []
    updates = {fid: dash.no_update for fid in _GRID_ALIGN_ALL_FIELD_IDS}
    for m in mismatches:
        fids = _GRID_ALIGN_FIELD_IDS.get(m["name"], ())
        if len(fids) == 1:
            updates[fids[0]] = round(max(m["actual"]), 6)
        else:
            for fid, val in zip(fids, m["actual"]):
                updates[fid] = round(val, 6)
    _pending_grid_fix["mismatches"] = None
    note = (current_status or "") + (" | Project settings updated to match the mesh grid - "
                                      "this project has changed and must be saved manually "
                                      "(File > Save Project) to keep the fix.")
    return (False, note) + tuple(updates[fid] for fid in _GRID_ALIGN_ALL_FIELD_IDS)


@app.callback(
    Output("grid-align-modal", "is_open", allow_duplicate=True),
    Input("grid-align-keep-btn", "n_clicks"),
    prevent_initial_call=True,
)
def _keep_grid_align_as_typed(n_clicks):
    _pending_grid_fix["mismatches"] = None
    return False


# Every field the sequential opening-alignment walk could possibly write
# to - Dash requires callback Outputs declared statically, so (like
# _GRID_ALIGN_ALL_FIELD_IDS above) every field defaults to no_update
# except the one the current conflict is actually about.
_SEQ_ALIGN_ALL_FIELD_IDS = [
    f"{prefix}-{suffix}"
    for prefix in ("inlet", "outlet", "inlet2", "outlet2")
    for suffix in ("size-w", "size-h", "y-input", "z-input")
]

_SEQ_ALIGN_SAVE_NOTE = (" | Grid-alignment fixes applied to the form - this project has changed "
                        "and must be saved manually (File > Save Project) to keep them.")


def _advance_alignment_walk(agree):
    """Send `agree` into the live walker (see _pending_alignment_walk),
    returning (still_open, body, field_updates: dict) - shared by the
    Agree/Decline callbacks below. field_updates is empty for a Decline
    (agree=False never writes anything - see walk_opening_alignment_conflicts's
    own docstring on what agreeing/declining means). still_open is False
    once the generator is exhausted, at which point the caller should
    check _pending_alignment_walk["any_applied"] to decide whether the
    "must save" reminder belongs in this response.
    """
    walker = _pending_alignment_walk.get("walker")
    current = _pending_alignment_walk.get("current")
    field_updates = {}
    if agree and current is not None:
        field_updates[current["field_id"]] = round(current["suggested"], 6)
        _pending_alignment_walk["any_applied"] = True
    if walker is None:
        return False, dash.no_update, field_updates
    try:
        next_conflict = walker.send(agree)
    except StopIteration:
        _pending_alignment_walk["walker"] = None
        _pending_alignment_walk["current"] = None
        return False, dash.no_update, field_updates
    _pending_alignment_walk["current"] = next_conflict
    return True, _grid_align_seq_body(next_conflict), field_updates


@app.callback(
    Output("grid-align-seq-modal", "is_open", allow_duplicate=True),
    Output("grid-align-seq-modal-body", "children", allow_duplicate=True),
    Output("project-status", "children", allow_duplicate=True),
    [Output(fid, "value", allow_duplicate=True) for fid in _SEQ_ALIGN_ALL_FIELD_IDS],
    Input("grid-align-seq-agree-btn", "n_clicks"),
    State("project-status", "children"),
    prevent_initial_call=True,
)
def _grid_align_seq_agree(n_clicks, current_status):
    still_open, body, field_updates = _advance_alignment_walk(True)
    updates = {fid: dash.no_update for fid in _SEQ_ALIGN_ALL_FIELD_IDS}
    updates.update(field_updates)
    status = current_status
    if not still_open and _pending_alignment_walk.get("any_applied"):
        status = (current_status or "") + _SEQ_ALIGN_SAVE_NOTE
        _pending_alignment_walk["any_applied"] = False
    # is_open only needs an explicit write on the FINAL step (closing it) -
    # already True while mid-walk, and writing True again on every
    # intermediate step would needlessly re-trigger
    # _open_bulk_modal_after_seq_modal_closes each time (harmless there,
    # since its own guard no-ops on seq_is_open=True, but wasteful).
    is_open = dash.no_update if still_open else False
    return (is_open, body, status) + tuple(updates[fid] for fid in _SEQ_ALIGN_ALL_FIELD_IDS)


@app.callback(
    Output("grid-align-seq-modal", "is_open", allow_duplicate=True),
    Output("grid-align-seq-modal-body", "children", allow_duplicate=True),
    Output("project-status", "children", allow_duplicate=True),
    Input("grid-align-seq-decline-btn", "n_clicks"),
    State("project-status", "children"),
    prevent_initial_call=True,
)
def _grid_align_seq_decline(n_clicks, current_status):
    still_open, body, _ = _advance_alignment_walk(False)
    status = current_status
    if not still_open and _pending_alignment_walk.get("any_applied"):
        status = (current_status or "") + _SEQ_ALIGN_SAVE_NOTE
        _pending_alignment_walk["any_applied"] = False
    is_open = dash.no_update if still_open else False
    return is_open, body, status


@app.callback(
    Output("grid-align-modal", "is_open", allow_duplicate=True),
    Output("grid-align-modal-body", "children", allow_duplicate=True),
    Input("grid-align-seq-modal", "is_open"),
    prevent_initial_call=True,
)
def _open_bulk_modal_after_seq_modal_closes(seq_is_open):
    """Opens the (now source-zone-only) bulk modal once the sequential
    opening-alignment walk finishes - deliberately decoupled from the
    Agree/Decline callbacks above so neither needs to know about the
    other's modal; see _open_project's own comment on why the bulk modal
    isn't opened immediately alongside the sequential one.
    """
    if seq_is_open:
        return dash.no_update, dash.no_update
    mismatches = _pending_grid_fix.get("mismatches") or []
    if not mismatches:
        return dash.no_update, dash.no_update
    return True, _grid_align_modal_body(mismatches)


def _load_extend_status(project_dir):
    """Load project_status.json for project_dir, plus (if it names a real
    guv_path/settings_path) the settings dict and Project/room needed to
    actually launch a new sweep from the "Extend / modify simulations"
    modal - mirrors _open_project's own load sequence, just without
    touching any of THIS app's currently-loaded project/form state (the
    folder picked here can be a different project entirely).

    Returns (status, settings, room, guv_path, settings_path, error) -
    error is a user-facing string, or None on success. An empty (never-
    swept) project_dir is NOT an error - status/combos may legitimately
    be empty; the error case is specifically "there's no status file AND
    no way to load settings for this folder at all".
    """
    project_name = Path(project_dir).name if project_dir else ""
    status = load_project_status(project_dir, project_name)
    if not status.get("combos") and not status.get("guv_path"):
        return status, None, None, None, None, (
            "No recorded status for this folder yet - open its project via File > Open Project "
            "first (which creates one), or point this at a folder a sweep has already run in."
        )
    guv_path, settings_path = status.get("guv_path"), status.get("settings_path")
    if not guv_path or not settings_path:
        return status, None, None, None, None, (
            "This folder's status file is missing its guv_path/settings_path - re-open the "
            "project via File > Open Project to backfill it."
        )
    try:
        with open(settings_path) as f:
            settings = json.load(f)
        project = Project.load(guv_path)
        room = next(iter(project.rooms.values()))
    except Exception as e:
        return status, None, None, guv_path, settings_path, f"Failed to load {guv_path}/{settings_path}: {e}"
    return status, settings, room, guv_path, settings_path, None


def _refresh_extend_view(project_dir):
    """(Re)load project_dir's status into _pending_extend_modal and
    compute everything the modal body needs to render - shared by the
    Open and Load buttons (see their own callbacks below), since loading
    a folder means the same thing from either entry point.
    """
    status, settings, room, guv_path, settings_path, error = _load_extend_status(project_dir)
    project_name = Path(project_dir).name if project_dir else None
    _pending_extend_modal.update(
        project_dir=project_dir, project_name=project_name, guv_path=guv_path,
        settings_path=settings_path, settings=settings, room=room, status=status, action=None,
    )
    combos = (status or {}).get("combos") or {}
    z_values = sorted({c["z"] for c in combos.values() if "z" in c})
    ach_values = sorted({c["ach"] for c in combos.values() if "ach" in c})
    dropdown_options = [
        {"label": f"Z={c.get('z')} / ACH={c.get('ach')} ({c.get('status')})", "value": key}
        for key, c in sorted(combos.items())
    ]
    sweep_z = ", ".join(f"{v:g}" for v in z_values)
    sweep_ach = ", ".join(f"{v:g}" for v in ach_values)
    body = _extend_modal_body(status, project_dir, error)
    # Offer the rebuild-from-disk option specifically when there's no
    # status file to work with AT ALL (no combos, no guv_path) - not for
    # the other error cases above (a status file that names a guv_path/
    # settings_path that's since gone missing isn't something rescanning
    # combo subfolders would fix).
    no_status_at_all = not combos and not (status or {}).get("guv_path")
    rebuild_style = {"display": "block"} if (project_dir and no_status_at_all) else {"display": "none"}
    # Only offered for a steady-state project - see the dropdown's own
    # note in the layout for why the reverse direction isn't.
    mode_row_style = ({"display": "block"} if (settings or {}).get("sim-type") == "steady_state"
                       else {"display": "none"})
    # Prefilled with this project's own Z values (not left blank) so the
    # user sees exactly which ones are about to be re-evaluated under a
    # new .guv design, rather than having to remember/guess what leaving
    # it empty defaults to.
    return (body, (error or ""), sweep_z, sweep_ach, sweep_z, (guv_path or ""), dropdown_options, rebuild_style,
            mode_row_style)


@app.callback(
    Output("extend-modal", "is_open", allow_duplicate=True),
    Output("extend-project-dir-input", "value"),
    Output("extend-status-table", "children", allow_duplicate=True),
    Output("extend-action-msg", "children", allow_duplicate=True),
    Output("extend-sweep-z-values", "value", allow_duplicate=True),
    Output("extend-sweep-ach-values", "value", allow_duplicate=True),
    Output("extend-uv-z-values", "value", allow_duplicate=True),
    Output("extend-uv-guv-path", "value", allow_duplicate=True),
    Output("extend-combo-dropdown", "options", allow_duplicate=True),
    Output("extend-rebuild-status-wrapper", "style", allow_duplicate=True),
    Output("extend-uv-mode-row", "style", allow_duplicate=True),
    Input("extend-modal-open-btn", "n_clicks"),
    State("case-dir", "value"),
    prevent_initial_call=True,
)
def _open_extend_modal(n_clicks, current_case_dir):
    project_dir = current_case_dir or ""
    body, msg, sweep_z, sweep_ach, uv_z, uv_guv, options, rebuild_style, mode_style = _refresh_extend_view(
        project_dir)
    return True, project_dir, body, msg, sweep_z, sweep_ach, uv_z, uv_guv, options, rebuild_style, mode_style


@app.callback(
    Output("extend-status-table", "children", allow_duplicate=True),
    Output("extend-action-msg", "children", allow_duplicate=True),
    Output("extend-sweep-z-values", "value", allow_duplicate=True),
    Output("extend-sweep-ach-values", "value", allow_duplicate=True),
    Output("extend-uv-z-values", "value", allow_duplicate=True),
    Output("extend-uv-guv-path", "value", allow_duplicate=True),
    Output("extend-combo-dropdown", "options", allow_duplicate=True),
    Output("extend-rebuild-status-wrapper", "style", allow_duplicate=True),
    Output("extend-uv-mode-row", "style", allow_duplicate=True),
    Input("extend-load-btn", "n_clicks"),
    State("extend-project-dir-input", "value"),
    prevent_initial_call=True,
)
def _load_extend_project(n_clicks, project_dir):
    if not project_dir:
        return (dash.no_update, "Enter a project folder first.", dash.no_update, dash.no_update,
                dash.no_update, dash.no_update, dash.no_update, dash.no_update, dash.no_update)
    return _refresh_extend_view(project_dir)


@app.callback(
    Output("extend-status-table", "children", allow_duplicate=True),
    Output("extend-action-msg", "children", allow_duplicate=True),
    Output("extend-sweep-z-values", "value", allow_duplicate=True),
    Output("extend-sweep-ach-values", "value", allow_duplicate=True),
    Output("extend-uv-z-values", "value", allow_duplicate=True),
    Output("extend-uv-guv-path", "value", allow_duplicate=True),
    Output("extend-combo-dropdown", "options", allow_duplicate=True),
    Output("extend-rebuild-status-wrapper", "style", allow_duplicate=True),
    Output("extend-uv-mode-row", "style", allow_duplicate=True),
    Input("extend-rebuild-status-btn", "n_clicks"),
    State("extend-project-dir-input", "value"),
    prevent_initial_call=True,
)
def _create_extend_status_from_disk(n_clicks, project_dir):
    """Rebuild a project_status.json from the combo subfolders already on
    disk (see scenario_runs.rebuild_project_status_from_disk) - the
    action offered whenever _refresh_extend_view finds no status file at
    all for a loaded folder, so an older project (or one from before this
    feature existed) isn't a dead end just because nothing's been status-
    tracked for it yet.
    """
    _NA = dash.no_update
    if not project_dir:
        return _NA, "Enter a project folder first.", _NA, _NA, _NA, _NA, _NA, _NA, _NA
    guv_path = scenario_runs.find_first_guv_path_on_disk(project_dir)
    if not guv_path:
        return (_NA, "No existing run folders (with their own run_settings.json) were found in "
                "this location to rebuild a status file from.", _NA, _NA, _NA, _NA, _NA, _NA, _NA)
    try:
        project = Project.load(guv_path)
        room = next(iter(project.rooms.values()))
    except Exception as e:
        return _NA, f"Found run folders, but failed to load {guv_path}: {e}", _NA, _NA, _NA, _NA, _NA, _NA, _NA

    project_name = Path(project_dir).name
    n_found = scenario_runs.rebuild_project_status_from_disk(project_dir, project_name, room)
    body, msg, sweep_z, sweep_ach, uv_z, uv_guv, options, rebuild_style, mode_style = _refresh_extend_view(
        project_dir)
    prefix = f"Rebuilt a status file from {n_found} existing run folder(s)."
    return (body, (f"{prefix} {msg}" if msg else prefix), sweep_z, sweep_ach, uv_z, uv_guv, options,
            rebuild_style, mode_style)


@app.callback(
    Output("extend-project-dir-input", "value", allow_duplicate=True),
    Input("extend-browse-btn", "n_clicks"),
    State("extend-project-dir-input", "value"),
    prevent_initial_call=True,
)
def _browse_extend_project_dir(n_clicks, current_dir):
    path = _native_choose_dir("Choose a project folder", initialdir=current_dir)
    return path if path else dash.no_update


@app.callback(
    Output("extend-uv-guv-path", "value", allow_duplicate=True),
    Input("extend-uv-browse-btn", "n_clicks"),
    State("extend-uv-guv-path", "value"),
    prevent_initial_call=True,
)
def _browse_extend_uv_guv(n_clicks, current_guv_path):
    # current_guv_path (if set) names a FILE, not a directory - Tk's
    # initialdir needs the containing folder, same reasoning
    # _load_results already applies to its own file-picker field.
    initialdir = str(Path(current_guv_path).parent) if current_guv_path else None
    path = _native_open_file([("GUV-CFD project .guv files", "*.guv"), ("All files", "*.*")],
                              "Choose a .guv file", initialdir=initialdir)
    return path if path else dash.no_update


def _set_extend_action(action):
    """Record which of the 3 actions is selected and return the 3
    sections' styles (exactly one shown) - one tiny callback per button
    below calls this with its own fixed action, rather than one callback
    reading dash.ctx.triggered_id to figure out which button fired; same
    outcome, but each button's handler is trivial to call/test directly.
    """
    _pending_extend_modal["action"] = action
    hidden, shown = {"display": "none"}, {"display": "block"}
    return (shown if action == "uv" else hidden,
            shown if action == "extend" else hidden,
            shown if action == "sweep" else hidden)


@app.callback(
    Output("extend-uv-section", "style"),
    Output("extend-extend-section", "style"),
    Output("extend-sweep-section", "style"),
    Input("extend-action-uv-btn", "n_clicks"),
    prevent_initial_call=True,
)
def _select_extend_action_uv(n_clicks):
    return _set_extend_action("uv")


@app.callback(
    Output("extend-uv-section", "style", allow_duplicate=True),
    Output("extend-extend-section", "style", allow_duplicate=True),
    Output("extend-sweep-section", "style", allow_duplicate=True),
    Input("extend-action-extend-btn", "n_clicks"),
    prevent_initial_call=True,
)
def _select_extend_action_extend(n_clicks):
    return _set_extend_action("extend")


@app.callback(
    Output("extend-uv-section", "style", allow_duplicate=True),
    Output("extend-extend-section", "style", allow_duplicate=True),
    Output("extend-sweep-section", "style", allow_duplicate=True),
    Input("extend-action-sweep-btn", "n_clicks"),
    prevent_initial_call=True,
)
def _select_extend_action_sweep(n_clicks):
    return _set_extend_action("sweep")


@app.callback(
    Output("extend-modal", "is_open", allow_duplicate=True),
    Input("extend-cancel-btn", "n_clicks"),
    prevent_initial_call=True,
)
def _cancel_extend_modal(n_clicks):
    _pending_extend_modal.update(project_dir=None, project_name=None, guv_path=None, settings_path=None,
                                  settings=None, room=None, status=None, action=None)
    return False


# Holds the exact list of folders a cleanup click found, between that
# click and the confirm dialog's own answer - same single-user, module-
# level pending-state pattern as _pending_run/_pending_extend_modal.
_pending_extend_cleanup = {"dirs": None}


def _extend_ach_scratch_dirs(project_dir, status):
    """Every _base_ACH*/_control_ACH*/_phase1_ACH* folder this project's
    own recorded ach_bases entries point at, filtered to ones that still
    exist on disk - the exact, complete list of what a cleanup click
    would remove, computed up front so the confirmation prompt can name
    them explicitly rather than describing them vaguely.
    """
    ach_labels = sorted((status.get("ach_bases") or {}).keys())
    dirs = [f"{project_dir}/_base_ACH{label}" for label in ach_labels]
    dirs += [f"{project_dir}/_control_ACH{label}" for label in ach_labels]
    dirs += [f"{project_dir}/_phase1_ACH{label}" for label in ach_labels]  # steady-state only; no-op if absent
    return [d for d in dirs if Path(d).exists()]


@app.callback(
    Output("extend-cleanup-confirm", "displayed"),
    Output("extend-cleanup-confirm", "message"),
    Output("extend-action-msg", "children", allow_duplicate=True),
    Input("extend-cleanup-btn", "n_clicks"),
    prevent_initial_call=True,
)
def _prompt_extend_cleanup(n_clicks):
    project_dir = _pending_extend_modal.get("project_dir")
    if not project_dir:
        return False, dash.no_update, "Load a project folder first."
    status = _pending_extend_modal.get("status") or {}
    existing = _extend_ach_scratch_dirs(project_dir, status)
    if not existing:
        return False, dash.no_update, "No temporary flow-field folders found for this project - nothing to delete."
    _pending_extend_cleanup["dirs"] = existing
    names = ", ".join(Path(d).name for d in existing)
    message = (f"This will permanently delete {len(existing)} folder(s) in {project_dir}:\n\n{names}\n\n"
               f"These hold only reusable working copies of the flow field, never simulation "
               f"results - your Z/ACH combination folders and their results.json are NOT affected. "
               f"A future sweep will simply rebuild whatever it needs. Delete these now?")
    return True, message, dash.no_update


@app.callback(
    Output("extend-status-table", "children", allow_duplicate=True),
    Output("extend-action-msg", "children", allow_duplicate=True),
    Input("extend-cleanup-confirm", "submit_n_clicks"),
    prevent_initial_call=True,
)
def _confirm_extend_cleanup(submit_n_clicks):
    dirs = _pending_extend_cleanup.get("dirs")
    _pending_extend_cleanup["dirs"] = None
    project_dir = _pending_extend_modal.get("project_dir")
    project_name = _pending_extend_modal.get("project_name")
    if not dirs or not project_dir:
        return dash.no_update, dash.no_update
    quoted = " ".join(f'"{wsl_path(d)}"' for d in dirs)
    run_wsl_or_raise(f"rm -rf {quoted}", wsl_path(project_dir), "deleting temporary flow-field folders")
    clear_ach_bases(project_dir, project_name)
    new_status = load_project_status(project_dir, project_name)
    _pending_extend_modal["status"] = new_status
    return _extend_status_table(new_status, project_dir), f"Deleted {len(dirs)} temporary flow-field folder(s)."


def _extend_pipeline_thread(case_dirs, end_time, write_interval):
    """Sequentially extend each decay case_dir to end_time - not folded
    into run_sweep/run_decay_sweep's own shared solver pool, since this is
    a small, explicit, user-picked list rather than a Z x ACH cross-product (see the plan's own
    "Extend specific run(s)" design). Reuses _scenario_state/_scenario_log
    so progress shows on the same Run Simulations tab the ordinary sweep
    path already renders to.
    """
    _scenario_state.update(status="running", log=[], combos=[], results={},
                            start_time=time.time(), stop_requested=False, live_status={})
    for case_dir, z, ach in case_dirs:
        _scenario_state["combos"].append((z, ach))
        _scenario_log(f"[Z={z}/ACH={ach}] Extending to {end_time}s...")
        try:
            _continue_decay(case_dir, end_time, write_interval)
            _scenario_state["results"][(z, ach)] = {"status": "done", "detail": None}
            _scenario_log(f"[Z={z}/ACH={ach}] Done.")
        except Exception as e:
            _scenario_log(f"[Z={z}/ACH={ach}] ERROR: {e}")
            _scenario_state["results"][(z, ach)] = {"status": "error", "detail": str(e)}
    _scenario_state["status"] = "done"


@app.callback(
    Output("extend-modal", "is_open", allow_duplicate=True),
    Output("extend-action-msg", "children", allow_duplicate=True),
    Output("main-tabs", "active_tab", allow_duplicate=True),
    # Without these, a launch from this modal starts the sweep/extend job
    # just fine server-side (_scenario_state/_run_state genuinely go
    # "running"), but the Run Simulations tab's own log/live-status/table
    # never update - scenario-poll's dcc.Interval keeps whatever
    # disabled state it already had (there's no OTHER trigger that would
    # flip it), so nothing appears to happen until a second launch attempt
    # gets rejected with "already running" - confirmed as a real incident.
    # scenario-run-btn/scenario-stop-btn mirror _start_scenario_sweep's own
    # multi-combo-launch outputs, so the Run Simulations tab looks exactly
    # as "mid-sweep" as it would from its own Start button.
    Output("scenario-poll", "disabled", allow_duplicate=True),
    Output("scenario-run-btn", "disabled", allow_duplicate=True),
    Output("scenario-stop-btn", "disabled", allow_duplicate=True),
    Input("extend-run-btn", "n_clicks"),
    State("extend-uv-guv-path", "value"),
    State("extend-uv-z-values", "value"),
    State("extend-uv-sim-type", "value"),
    State("extend-combo-dropdown", "value"),
    State("extend-duration-input", "value"),
    State("extend-sweep-z-values", "value"),
    State("extend-sweep-ach-values", "value"),
    prevent_initial_call=True,
)
def _run_extend_action(n_clicks, uv_guv_path, uv_z_text, uv_sim_type, extend_combo_keys, extend_end_time,
                        sweep_z_text, sweep_ach_text):
    _NA = dash.no_update
    pending = _pending_extend_modal
    action = pending.get("action")
    project_dir, settings, room = pending.get("project_dir"), pending.get("settings"), pending.get("room")
    if not project_dir or settings is None or room is None:
        return _NA, "Load a valid project folder first.", _NA, _NA, _NA, _NA
    if _scenario_state["status"] == "running" or _run_state["status"] == "running":
        return _NA, "A simulation is already running - stop it first.", _NA, _NA, _NA, _NA

    combos = (pending.get("status") or {}).get("combos") or {}
    adv = merge_project_openfoam_settings(settings, load_advanced_settings())

    if action == "uv":
        if not uv_guv_path:
            return _NA, "Pick a .guv file first.", _NA, _NA, _NA, _NA
        try:
            uv_z_values = _parse_number_list(uv_z_text)
        except ValueError as e:
            return _NA, f"Can't parse Z value list: {e}", _NA, _NA, _NA, _NA
        # Load THIS design's own room fresh - `room` (from pending) is
        # whatever was loaded for the ORIGINAL project (see
        # _refresh_extend_view), and the whole point of this action is a
        # DIFFERENT lamp design/room object. Confirmed as a real bug
        # (2026-08-12): passing the original room through unchanged meant
        # every "apply different design" launch silently recomputed
        # fluenceRate from the SAME (original) lamp positions regardless
        # of which .guv was actually picked - two genuinely different
        # designs produced identical results because the physics never
        # actually changed, only the file path label did.
        try:
            uv_room = next(iter(Project.load(uv_guv_path).rooms.values()))
        except Exception as e:
            return _NA, f"Failed to load {uv_guv_path}: {e}", _NA, _NA, _NA, _NA
        # Blank field - use every Z value already recorded for this project
        # (see the note in the modal itself) rather than requiring the user
        # to retype a list they already swept once.
        z_values = uv_z_values or sorted({c["z"] for c in combos.values() if "z" in c}) or [settings.get("z-value")]
        ach_values = sorted({c["ach"] for c in combos.values() if "ach" in c}) or [settings.get("ach")]
        # The mode dropdown is only shown (and only meaningful) for a
        # steady-state project (see the layout's own note - switching
        # FROM decay isn't offered) - if this project isn't steady-state,
        # ignore uv_sim_type entirely rather than trust the hidden
        # dropdown's own default value, which would otherwise silently
        # flip an already-decay project back to steady-state.
        current_sim_type = settings.get("sim-type")
        sim_type = uv_sim_type if (current_sim_type == "steady_state" and uv_sim_type) else current_sim_type
        _launch_scenario_sweep(uv_guv_path, pending["settings_path"], project_dir, uv_room,
                                dict(settings, **{"z-value": z_values[0], "sim-type": sim_type}),
                                adv, z_values, ach_values)
    elif action == "sweep":
        try:
            z_values = _parse_number_list(sweep_z_text)
            ach_values = _parse_number_list(sweep_ach_text)
        except ValueError as e:
            return _NA, f"Can't parse Z/ACH list: {e}", _NA, _NA, _NA, _NA
        if not z_values or not ach_values:
            return _NA, "Enter at least one Z value and one ACH value.", _NA, _NA, _NA, _NA
        _launch_scenario_sweep(pending["guv_path"], pending["settings_path"], project_dir, room,
                                settings, adv, z_values, ach_values)
    elif action == "extend":
        if not extend_combo_keys:
            return _NA, "Pick at least one combination to extend.", _NA, _NA, _NA, _NA
        if extend_end_time is None:
            return _NA, "Enter a new end time first.", _NA, _NA, _NA, _NA
        if settings.get("sim-type") != "decay":
            return _NA, "Extending is only supported for decay projects in this version.", _NA, _NA, _NA, _NA
        write_interval = max(1, settings.get("pimple-write-interval", 1))
        case_dirs = []
        for key in extend_combo_keys:
            combo = combos.get(key)
            if not combo:
                continue
            z, ach = combo["z"], combo["ach"]
            # combo["subdir"], if recorded, is this combo's OWN actual
            # folder name - use it verbatim rather than recomputing from
            # (z, ach) alone, which would silently drop this combo's own
            # combo_suffix (see scenario_runs._subdir_name's docstring) and
            # point at the wrong - possibly a different design's - folder.
            subdir = combo.get("subdir") or scenario_runs._subdir_name(z, ach)
            case_dirs.append((f"{project_dir}/{subdir}", z, ach))
        thread = threading.Thread(target=_extend_pipeline_thread,
                                   args=(case_dirs, extend_end_time, write_interval), daemon=True)
        thread.start()
        _pending_extend_modal.update(project_dir=None, project_name=None, guv_path=None, settings_path=None,
                                      settings=None, room=None, status=None, action=None)
        return False, "", "scenario-runs", False, True, False
    else:
        return _NA, "Choose one of the 3 actions above first.", _NA, _NA, _NA, _NA

    _pending_extend_modal.update(project_dir=None, project_name=None, guv_path=None, settings_path=None,
                                  settings=None, room=None, status=None, action=None)
    return False, "", "scenario-runs", False, True, False


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
    # For _single_run_progress_table's benefit (see _start_scenario_sweep's
    # 1-combination branch) - the Run Simulations tab needs a Z/ACH to show
    # even for a run launched this way, not just Processing's own display.
    _run_state["z"] = settings.get("z-value")
    _run_state["ach"] = settings.get("ach")
    thread = threading.Thread(
        target=_run_pipeline_thread,
        args=(sim_type, guv_path, case_dir, room, settings),
        daemon=True,
    )
    thread.start()


def _scenario_sweep_thread(guv_path, settings_path, project_dir, room, settings, adv, z_values, ach_values):
    def on_combo_done(z, ach, status, detail):
        _scenario_state["results"][(z, ach)] = {"status": status, "detail": detail}

    is_decay = settings.get("sim-type") == "decay"
    sweep_fn = scenario_runs.run_decay_sweep if is_decay else scenario_runs.run_sweep
    try:
        sweep_fn(
            guv_path, settings_path, project_dir, room, settings, adv,
            z_values, ach_values, log_fn=_scenario_log, should_stop=_scenario_should_stop,
            should_pause=_scenario_should_pause,
            on_combo_done=on_combo_done, status_fn=_scenario_status_update,
            # Without this, _run_phase()'s on_line=solver_log_fn or log_fn
            # falls back to log_fn - every raw per-iteration solver line
            # (residuals, "Time = N" banners) would flood the scenario
            # log. Mirrors _run_steady_state's use of _track_solver_time,
            # but scenario runs don't have a single "current time" to
            # track (many combinations interleave), so this discards
            # everything EXCEPT run_wsl_streaming's own "[...]"-wrapped
            # stall/retry diagnostics (see its docstring) - those still
            # need to reach the visible scenario log, or a stalled/killed
            # solver goes completely unnoticed here too.
            solver_log_fn=lambda line: _scenario_log(line) if line.strip().startswith("[") else None,
        )
        _scenario_state["status"] = "done"
    except StoppedByUser as e:
        _scenario_log(f"Stopped: {e}")
        _scenario_state["status"] = "stopped"
    except Exception as e:
        _scenario_log(f"ERROR: {e}")
        _scenario_state["status"] = "error"
    finally:
        # Belt-and-suspenders: every stream already clears its own
        # live_status entry when it finishes (see _run_shared_control/
        # _run_decay_scenario's try/finally), but wipe the whole thing
        # here too so nothing stale can survive the sweep ending.
        _scenario_state["live_status"].clear()


def _launch_scenario_sweep(guv_path, settings_path, project_dir, room, settings, adv, z_values, ach_values):
    combos = scenario_runs.sweep_combinations(z_values, ach_values)
    _scenario_state.update(status="running", log=[], combos=combos, results={},
                            start_time=time.time(), stop_requested=False, pause_requested=False, live_status={})
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
        return True, True, False, dash.no_update, "scenario-runs", False, dash.no_update

    room = _loaded["room"]
    guv_path = _loaded["path"]
    if room is None or guv_path is None:
        return (False, False, True, "No .guv project loaded - use File > Open Project or "
                "Load .guv file first.", dash.no_update, False, dash.no_update)

    settings = _collect_settings(values)
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

    sealed_error = _sealed_room_error(sim_type, settings["ach"], settings.get("fan-enable"))
    if sealed_error:
        return (False, False, True, sealed_error, dash.no_update, False, dash.no_update)
    mech_ach_only_error = _mechanical_ach_only_error(sim_type, settings["ach"], settings.get("mech-ach-only"))
    if mech_ach_only_error:
        return (False, False, True, mech_ach_only_error, dash.no_update, False, dash.no_update)

    # A case whose flow convergence paused (FlowConvergenceUndecided) and
    # was never resolved - possibly in an earlier server session that no
    # longer has any memory of it (the whole point of persisting chunk
    # history to disk - see case_awaiting_flow_decision's docstring).
    # Launching a fresh Run here would silently regenerate the mesh and
    # destroy that paused progress - _case_dir_has_data() below wouldn't
    # even catch this case (a paused flow convergence leaves no
    # results.json and no leftover time directories, both cleaned up after
    # every chunk), so this check must come first.
    adv = merge_project_openfoam_settings(settings, load_advanced_settings())
    pending = case_awaiting_flow_decision(
        case_dir, oscillation_window=adv["oscillation-window"],
        oscillation_growth_tol=adv["oscillation-growth-tol"],
        rel_tol=adv["flow-rel-tol"] / 100.0)
    if pending:
        _run_log(f"Found a paused flow convergence in {case_dir} from an earlier session "
                 f"({pending['total_iterations']} iterations so far) - awaiting your decision "
                 f"instead of starting a fresh run.")
        _run_state["status"] = "awaiting_decision"
        _run_state["decision"] = {
            "sim_type": sim_type, "guv_path": guv_path, "case_dir": case_dir, "room": room,
            "settings": settings, "diagnostic": pending["diagnostic"],
            "total_iterations": pending["total_iterations"], "started_at": datetime.now(), "start": time.time(),
            "kind": "flow",
        }
        return True, True, False, "", "scenario-runs", False, dash.no_update

    # A steady-state case whose setup fully completed (mesh + flow
    # convergence, possibly Phase 1 too) but never reached results.json -
    # same reasoning as the flow-convergence check above: a fresh Run here
    # would silently regenerate the mesh and destroy that progress, and
    # _case_dir_has_data() below can't be trusted to catch it (Phase 1's own
    # cleanup can leave the case dir looking mostly like "just 0/"), so this
    # must also come before the generic overwrite check.
    resume_info = case_awaiting_phase2_resume(case_dir)
    if resume_info:
        if resume_info["phase1_done"]:
            detail = (f"Phase 1 of the steady-state scenario already converged "
                      f"({resume_info['phase1_iterations']} iterations) - resuming will skip "
                      f"straight into Phase 2.")
        else:
            detail = ("Phase 1 hadn't converged yet when this run stopped - resuming will redo "
                      "Phase 1, but mesh generation and flow convergence (already done, and the "
                      "more expensive steps) won't be repeated.")
        _run_log(f"Found an unfinished steady-state run in {case_dir} from an earlier session - "
                 f"awaiting your decision instead of starting a fresh run. {detail}")
        _run_state["status"] = "awaiting_phase2_resume"
        _run_state["phase2_decision"] = {
            "sim_type": sim_type, "guv_path": guv_path, "case_dir": case_dir, "room": room,
            "settings": settings, "detail": detail, "started_at": datetime.now(), "start": time.time(),
        }
        return True, True, False, "", "scenario-runs", False, dash.no_update

    if _case_dir_has_data(case_dir):
        _pending_run.update(sim_type=sim_type, guv_path=guv_path, case_dir=case_dir,
                             room=room, settings=settings)
        return (False, False, True, "", dash.no_update, True,
                f"{case_dir} already has simulation data (results.json and/or solver "
                f"output). Running will regenerate the mesh and overwrite the case "
                f"directory in place - existing results may be lost. Continue anyway?")

    _launch_run(sim_type, guv_path, case_dir, room, settings)
    return True, True, False, "", "scenario-runs", False, dash.no_update


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
    return True, True, False, "scenario-runs"


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
        return True, True, False, dash.no_update, "scenario-runs"

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
    return True, True, False, "", "scenario-runs"


def _start_flow_decision(action, additional_iterations, mesh_values):
    """Shared by the Continue/Accept buttons on the flow-decision panel
    (see FlowConvergenceUndecided/Phase1ExtrapolationUndecided) - validates
    the same way the existing "Continue to longer duration" button does
    (_settings_mismatch against run_settings.json, since either resume path
    reuses the mesh/BCs on disk as-is and would silently ignore any GUI
    changes made since the pause), then launches the resume thread that
    matches this decision's "kind" (flow convergence vs Phase 1
    extrapolation - same panel, different underlying resume mechanism).
    """
    if _run_state["status"] != "awaiting_decision" or not _run_state.get("decision"):
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update, dash.no_update, dash.no_update

    decision = _run_state["decision"]
    case_dir = decision["case_dir"]
    kind = decision.get("kind", "flow")
    mismatches = _settings_mismatch(case_dir, dict(zip(_MESH_AFFECTING_FIELDS, mesh_values)))
    if mismatches:
        changed = "; ".join(f"{field} was {prior}, now {current}" for field, prior, current in mismatches)
        what = "continues flow convergence" if kind == "flow" else "continues Phase 1"
        return (dash.no_update, False, False, True,
                f"These settings differ from the run currently on disk, and resuming won't apply "
                f"them (it only {what} - mesh/BCs are reused as-is): {changed}. "
                f"Run a full simulation instead if you want these changes to take effect.",
                dash.no_update)

    label = "flow convergence" if kind == "flow" else "Phase 1"
    if action == "continue":
        if not additional_iterations or additional_iterations <= 0:
            return (dash.no_update, False, False, True,
                    "Enter a positive number of iterations to continue.", dash.no_update)
        _run_log(f"Continuing {label} for {additional_iterations} more iterations (user decision)...")
    else:
        _run_log(f"Accepting current {label} state as-is (user decision) and continuing...")

    _run_state["status"] = "running"
    target = _resume_pipeline_thread if kind == "flow" else _resume_phase1_extrapolation_thread
    thread = threading.Thread(target=target, args=(action, additional_iterations), daemon=True)
    thread.start()
    return {"display": "none"}, True, True, False, "", "scenario-runs"


@app.callback(
    Output("flow-decision-panel", "style", allow_duplicate=True),
    Output("run-btn", "disabled", allow_duplicate=True),
    Output("continue-btn", "disabled", allow_duplicate=True),
    Output("run-poll", "disabled", allow_duplicate=True),
    Output("run-validation-msg", "children", allow_duplicate=True),
    Output("main-tabs", "active_tab", allow_duplicate=True),
    Input("flow-decision-continue-btn", "n_clicks"),
    State("flow-decision-iterations", "value"),
    [State(fid, "value") for fid in _MESH_AFFECTING_FIELDS],
    prevent_initial_call=True,
)
def _start_flow_decision_continue(n_clicks, additional_iterations, *mesh_values):
    return _start_flow_decision("continue", additional_iterations, mesh_values)


@app.callback(
    Output("flow-decision-panel", "style", allow_duplicate=True),
    Output("run-btn", "disabled", allow_duplicate=True),
    Output("continue-btn", "disabled", allow_duplicate=True),
    Output("run-poll", "disabled", allow_duplicate=True),
    Output("run-validation-msg", "children", allow_duplicate=True),
    Output("main-tabs", "active_tab", allow_duplicate=True),
    Input("flow-decision-accept-btn", "n_clicks"),
    [State(fid, "value") for fid in _MESH_AFFECTING_FIELDS],
    prevent_initial_call=True,
)
def _start_flow_decision_accept(n_clicks, *mesh_values):
    return _start_flow_decision("accept", None, mesh_values)


@app.callback(
    Output("flow-decision-panel", "style", allow_duplicate=True),
    Output("run-status-text", "children", allow_duplicate=True),
    Input("flow-decision-stop-btn", "n_clicks"),
    prevent_initial_call=True,
)
def _stop_flow_decision(n_clicks):
    if _run_state["status"] != "awaiting_decision":
        return dash.no_update, dash.no_update
    kind = (_run_state.get("decision") or {}).get("kind", "flow")
    label = "flow convergence" if kind == "flow" else "Phase 1"
    _run_log(f"Stopped at your request - the case directory is untouched, exactly as it was when "
             f"{label} paused. Come back to it any time via Run; nothing is lost.")
    _run_state["status"] = "stopped"
    _run_state["decision"] = None
    return {"display": "none"}, f"Stopped ({label} decision deferred)."


@app.callback(
    Output("phase2-resume-panel", "style", allow_duplicate=True),
    Output("run-btn", "disabled", allow_duplicate=True),
    Output("continue-btn", "disabled", allow_duplicate=True),
    Output("run-poll", "disabled", allow_duplicate=True),
    Output("run-validation-msg", "children", allow_duplicate=True),
    Output("main-tabs", "active_tab", allow_duplicate=True),
    Input("phase2-resume-btn", "n_clicks"),
    [State(fid, "value") for fid in _MESH_AFFECTING_FIELDS],
    prevent_initial_call=True,
)
def _start_phase2_resume(n_clicks, *mesh_values):
    if _run_state["status"] != "awaiting_phase2_resume" or not _run_state.get("phase2_decision"):
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update, dash.no_update, dash.no_update

    case_dir = _run_state["phase2_decision"]["case_dir"]
    # Same guard as _start_flow_decision - resuming reuses the mesh/flow
    # field/UV zones already on disk exactly as they were left, so any GUI
    # change to a mesh-affecting field since the earlier attempt would be
    # silently ignored (not applied) rather than actually taking effect.
    mismatches = _settings_mismatch(case_dir, dict(zip(_MESH_AFFECTING_FIELDS, mesh_values)))
    if mismatches:
        changed = "; ".join(f"{field} was {prior}, now {current}" for field, prior, current in mismatches)
        return (dash.no_update, False, False, True,
                f"These settings differ from the run currently on disk, and resuming won't apply "
                f"them (it reuses the existing mesh, flow field, and UV zones as-is): {changed}. "
                f"Discard and run a full simulation instead if you want these changes to take effect.",
                dash.no_update)

    _run_log("Resuming the unfinished steady-state run (user decision) - reusing the existing mesh, "
             "flow field, and UV zones as-is, skipping straight to where it left off...")
    _run_state["status"] = "running"
    thread = threading.Thread(target=_resume_phase2_thread, daemon=True)
    thread.start()
    return {"display": "none"}, True, True, False, "", "scenario-runs"


@app.callback(
    Output("phase2-resume-panel", "style", allow_duplicate=True),
    Output("run-status-text", "children", allow_duplicate=True),
    Input("phase2-discard-btn", "n_clicks"),
    prevent_initial_call=True,
)
def _discard_phase2_resume(n_clicks):
    if _run_state["status"] != "awaiting_phase2_resume":
        return dash.no_update, dash.no_update
    case_dir = _run_state["phase2_decision"]["case_dir"]
    _clear_setup_summary(case_dir)
    _clear_phase1_checkpoint(case_dir)
    _run_log(f"Discarded the unfinished run's checkpoint in {case_dir} at your request. Press Run "
             f"to start fresh - this will regenerate the mesh and overwrite the case directory.")
    _run_state["status"] = "stopped"
    _run_state["phase2_decision"] = None
    return {"display": "none"}, "Stopped (unfinished run discarded)."


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
    Output("run-log", "children", allow_duplicate=True),
    Input("pause-btn", "n_clicks"),
    prevent_initial_call=True,
)
def _toggle_pause_run(n_clicks):
    if _run_state["status"] != "running":
        return (dash.no_update,)
    if _run_state.get("pause_requested"):
        _run_state["pause_requested"] = False
        _run_log("Continue requested - resuming the active solver process...")
    else:
        _run_state["pause_requested"] = True
        _run_log("Pause requested - suspending the active solver process in place "
                 "(no iterations lost)...")
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
    # A 1-combination "sweep" routes to the exact same single-run mechanism
    # Processing has always used (_launch_run, plus its paused-flow/
    # unfinished-Phase2/overwrite checks) instead of scenario_runs.run_sweep -
    # preserves flow-convergence/Phase1-extrapolation decision-panel support,
    # which the plain sweep path has no equivalent for (a paused decision
    # there just fails that combo, see run_sweep's own docstring). These
    # outputs mirror _start_run's own for exactly that branch; every one is
    # allow_duplicate since _start_run's callback is still the primary
    # registration for each (kept, though its own button is now hidden).
    Output("run-btn", "disabled", allow_duplicate=True),
    Output("continue-btn", "disabled", allow_duplicate=True),
    Output("run-poll", "disabled", allow_duplicate=True),
    Output("run-validation-msg", "children", allow_duplicate=True),
    Output("overwrite-confirm", "displayed", allow_duplicate=True),
    Output("overwrite-confirm", "message", allow_duplicate=True),
    # Cleared (not left stale) specifically when a genuine multi-combo sweep
    # launches - see that branch below for why: nothing else ever populates
    # these for a multi-combo sweep (only the 1-combo path, via _poll_run,
    # auto-loads its own single result), so without this, Analysis silently
    # kept showing whatever unrelated result happened to be loaded from
    # before the sweep started - confirmed live ("shows results from a run
    # that has nothing to do with the sweep").
    Output("results-data", "data", allow_duplicate=True),
    Output("results-case-dir", "data", allow_duplicate=True),
    Input("scenario-run-btn", "n_clicks"),
    State("scenario-z-values", "value"),
    State("scenario-ach-values", "value"),
    [State(fid, "value") for fid in SETTINGS_FIELDS],
    prevent_initial_call=True,
)
def _start_scenario_sweep(n_clicks, z_text, ach_text, *values):
    # 8 dash.no_update placeholders for the single-run-only/results outputs
    # below, reused on every early return that doesn't take the 1-combo or
    # multi-combo launch branch.
    _NA = dash.no_update
    if _scenario_state["status"] == "running" or _run_state["status"] == "running":
        return True, False, False, dash.no_update, dash.no_update, _NA, _NA, _NA, _NA, _NA, _NA, _NA, _NA

    room = _loaded["room"]
    guv_path = _loaded["path"]
    if room is None or guv_path is None:
        return (False, True, True, "No .guv project loaded - use File > Open Project or "
                "Load .guv file first.", dash.no_update, _NA, _NA, _NA, _NA, _NA, _NA, _NA, _NA)

    settings = _collect_settings(values)
    if not settings.get("case-dir"):
        return (False, True, True, "Set an OpenFOAM project directory first.", dash.no_update,
                _NA, _NA, _NA, _NA, _NA, _NA, _NA, _NA)

    try:
        z_values = _parse_number_list(z_text)
        ach_values = _parse_number_list(ach_text)
    except ValueError as e:
        return (False, True, True, f"Can't parse Z/ACH list: {e}", dash.no_update,
                _NA, _NA, _NA, _NA, _NA, _NA, _NA, _NA)
    if not z_values or not ach_values:
        return (False, True, True, "Enter at least one Z value and one ACH value.", dash.no_update,
                _NA, _NA, _NA, _NA, _NA, _NA, _NA, _NA)

    missing = _validate_settings(settings)
    if missing:
        return (False, True, True,
                "Missing required value(s) - fill these in on Project Setup before running: "
                + ", ".join(missing) + ".", dash.no_update, _NA, _NA, _NA, _NA, _NA, _NA, _NA, _NA)

    sealed_error = _sealed_room_error(settings.get("sim-type"), ach_values, settings.get("fan-enable"))
    if sealed_error:
        return (False, True, True, sealed_error, dash.no_update, _NA, _NA, _NA, _NA, _NA, _NA, _NA, _NA)
    mech_ach_only_error = _mechanical_ach_only_error(
        settings.get("sim-type"), ach_values, settings.get("mech-ach-only"))
    if mech_ach_only_error:
        return (False, True, True, mech_ach_only_error, dash.no_update, _NA, _NA, _NA, _NA, _NA, _NA, _NA, _NA)

    combos = scenario_runs.sweep_combinations(z_values, ach_values)
    adv = merge_project_openfoam_settings(settings, load_advanced_settings())
    if len(combos) != 1:
        _launch_scenario_sweep(guv_path, _loaded.get("settings_path"), settings["case-dir"],
                                room, settings, adv, z_values, ach_values)
        return True, False, False, "", "scenario-runs", _NA, _NA, _NA, _NA, _NA, _NA, None, None

    # Exactly 1 combination - single-run path (see the Output block's own
    # comment). ach/z-value themselves stay hidden/unused elsewhere - this
    # single value is threaded straight into `settings` instead.
    z, ach = combos[0]
    settings = dict(settings, ach=ach, **{"z-value": z})
    case_dir = settings["case-dir"]
    sim_type = settings["sim-type"]

    pending = case_awaiting_flow_decision(
        case_dir, oscillation_window=adv["oscillation-window"],
        oscillation_growth_tol=adv["oscillation-growth-tol"],
        rel_tol=adv["flow-rel-tol"] / 100.0)
    if pending:
        _run_log(f"Found a paused flow convergence in {case_dir} from an earlier session "
                 f"({pending['total_iterations']} iterations so far) - awaiting your decision "
                 f"instead of starting a fresh run.")
        _run_state["status"] = "awaiting_decision"
        _run_state["decision"] = {
            "sim_type": sim_type, "guv_path": guv_path, "case_dir": case_dir, "room": room,
            "settings": settings, "diagnostic": pending["diagnostic"],
            "total_iterations": pending["total_iterations"], "started_at": datetime.now(), "start": time.time(),
            "kind": "flow",
        }
        return True, False, False, "", "scenario-runs", True, True, False, "", False, _NA, _NA, _NA

    resume_info = case_awaiting_phase2_resume(case_dir)
    if resume_info:
        if resume_info["phase1_done"]:
            detail = (f"Phase 1 of the steady-state scenario already converged "
                      f"({resume_info['phase1_iterations']} iterations) - resuming will skip "
                      f"straight into Phase 2.")
        else:
            detail = ("Phase 1 hadn't converged yet when this run stopped - resuming will redo "
                      "Phase 1, but mesh generation and flow convergence (already done, and the "
                      "more expensive steps) won't be repeated.")
        _run_log(f"Found an unfinished steady-state run in {case_dir} from an earlier session - "
                 f"awaiting your decision instead of starting a fresh run. {detail}")
        _run_state["status"] = "awaiting_phase2_resume"
        _run_state["phase2_decision"] = {
            "sim_type": sim_type, "guv_path": guv_path, "case_dir": case_dir, "room": room,
            "settings": settings, "detail": detail, "started_at": datetime.now(), "start": time.time(),
        }
        return True, False, False, "", "scenario-runs", True, True, False, "", False, _NA, _NA, _NA

    if _case_dir_has_data(case_dir):
        _pending_run.update(sim_type=sim_type, guv_path=guv_path, case_dir=case_dir,
                             room=room, settings=settings)
        return (True, False, False, "", "scenario-runs", False, False, True, "", True,
                f"{case_dir} already has simulation data (results.json and/or solver "
                f"output). Running will regenerate the mesh and overwrite the case "
                f"directory in place - existing results may be lost. Continue anyway?", _NA, _NA)

    _launch_run(sim_type, guv_path, case_dir, room, settings)
    return True, False, False, "", "scenario-runs", True, True, False, "", False, _NA, _NA, _NA


@app.callback(
    Output("scenario-log", "children", allow_duplicate=True),
    Input("scenario-stop-btn", "n_clicks"),
    prevent_initial_call=True,
)
def _stop_scenario_sweep(n_clicks):
    # A 1-combination "sweep" runs on _run_state, not _scenario_state (see
    # _start_scenario_sweep) - Stop needs to act on whichever is actually
    # active.
    if _run_state["status"] == "running":
        _run_state["stop_requested"] = True
        _run_log("Stop requested...")
    elif _scenario_state["status"] == "running":
        _scenario_state["stop_requested"] = True
        _scenario_log("Stop requested - the sweep will stop before its next combination...")
    return (dash.no_update,)


# Live-status key suffixes (see steady_state_pipeline.run_steady_state_
# scenario's status_key1/status_key2, scenario_runs._run_shared_control/
# _run_decay_scenario) mapped to the same named stages the single-run
# checklist already uses - lets a currently-running combo's row show
# *which* stage it's in using data status_fn already provides, without
# needing new structured-progress plumbing (see the Est. time to finish
# column's own placeholder below, which does need that).
_LIVE_STAGE_SUFFIXES = [("Phase2", "Phase 2"), ("UV-on", "Decay sim"), ("Phase1", "Phase 1"),
                        ("control", "Flow field calc"), ("flow", "Flow field calc")]


def _combo_live_key(z, ach):
    """This combo's own currently-active live_status key, or None if
    nothing's running for it yet - shared by _combo_live_stage (the status
    label) and _combo_eta_text (the ETA, via this same key's stripped-down
    progress entry - see _scenario_status_update).

    Phase 1 and the control run are ACH-group-shared (see
    scenario_runs._run_shared_phase1/_run_shared_control) - their own
    status keys ("ACH={ach}/Phase1"/"ACH={ach}/control") never carry a Z
    at all, unlike Phase2/UV-on's per-combo "Z={z}/ACH={ach}/..." keys, so
    both need their own explicit equality check here rather than the
    startswith(prefix) check every per-combo phase already matches.
    """
    prefix = f"Z={z}/ACH={ach}/"
    for key in _scenario_state["live_status"]:
        if key.startswith(prefix) or key in (f"ACH={ach}/control", f"ACH={ach}/Phase1"):
            return key
    return None


def _combo_live_stage(z, ach):
    """Best-effort current stage for a combo with no results.json yet -
    None if it hasn't started (no matching key in live_status at all).
    """
    key = _combo_live_key(z, ach)
    if key is None:
        return None
    for suffix, label in _LIVE_STAGE_SUFFIXES:
        if key.endswith(suffix):
            return label
    return "Running"


def _eta_text_from_progress(progress):
    """Bare '3:45'/'almost done' ETA text from a progress-shaped dict (see
    _new_progress_entry) - "" if nothing's active yet, target/current time
    isn't known, or not enough of the current phase has elapsed to
    extrapolate a rate from. Shared by _combo_eta_text (sweep mode, one
    entry per combo - see _scenario_state["progress"]) and
    _single_run_progress_table (single-run mode, _run_state itself already
    has this same shape) so both progress tables' "Est. time to finish"
    column is formatted identically. Same math as _solver_eta_text (the
    Processing-tab-era full-sentence version, still used by the "Running
    now" line elsewhere), just returning the bare duration instead of a
    full sentence, for a table cell.
    """
    if not progress:
        return ""
    cur, target, phase_start = progress.get("current_time"), progress.get("target_time"), \
        progress.get("phase_start_time")
    if not cur or not target or not phase_start:
        return ""
    try:
        cur_val = float(cur)
    except (TypeError, ValueError):
        return ""
    elapsed = time.time() - phase_start
    if cur_val <= 0 or elapsed <= 0:
        return ""
    remaining = (target - cur_val) * (elapsed / cur_val)
    if remaining <= 0:
        return "almost done"
    return _format_mmss(remaining)


def _combo_eta_text(z, ach):
    """'Est. time to finish' cell text for a combo currently running (see
    _combo_live_key/_eta_text_from_progress) - "" if nothing's active for
    this combo at all.
    """
    key = _combo_live_key(z, ach)
    if key is None:
        return ""
    return _eta_text_from_progress(_scenario_state["progress"].get(key.rsplit("/", 1)[0]))


def _scenario_progress_table():
    combos = _scenario_state["combos"]
    if not combos:
        return html.Div("No sweep run yet.", className="small text-muted")
    results = _scenario_state["results"]
    header = html.Tr([
        html.Th("Z"), html.Th("ACH"), html.Th("Status"), html.Th("Est. time to finish"),
        html.Th("Total reduction %"), html.Th("Measured ACH eff. %"), html.Th("Measured UV eff. %"),
        html.Th("Mechanical mixing eff. %"), html.Th("Est. ACH /hr"), html.Th("Est. eACH /hr"),
    ])
    rows = [header]
    for z, ach in combos:
        entry = results.get((z, ach))
        metrics = {"total_reduction_pct": None, "ach_efficiency_pct": None, "uv_efficiency_pct": None,
                   "mechanical_mixing_efficiency_pct": None, "est_ach_per_hr": None, "est_each_per_hr": None}
        if entry is None:
            stage = _combo_live_stage(z, ach)
            status = stage if stage else "pending"
            est_time = _combo_eta_text(z, ach) if stage else ""
        elif entry["status"] == "done":
            status = "Finished"
            metrics = combo_summary_metrics(entry["detail"])
            est_time = ""
        else:
            status = f"error: {entry['detail']}"
            est_time = ""

        def _pct(v):
            return f"{v:.1f}%" if v is not None else ("" if entry is None else "n/a")

        def _rate(v):
            return f"{v:.4g} /hr" if v is not None else ("" if entry is None else "n/a")

        rows.append(html.Tr([
            html.Td(z), html.Td(ach), html.Td(status), html.Td(est_time),
            html.Td(_pct(metrics["total_reduction_pct"])), html.Td(_pct(metrics["ach_efficiency_pct"])),
            html.Td(_pct(metrics["uv_efficiency_pct"])), html.Td(_pct(metrics["mechanical_mixing_efficiency_pct"])),
            html.Td(_rate(metrics["est_ach_per_hr"])), html.Td(_rate(metrics["est_each_per_hr"])),
        ]))
    return dbc.Table(rows, bordered=False, hover=True, size="sm", className="small")


_RUN_STATE_ACTIVE_STATUSES = ("running", "awaiting_decision", "awaiting_phase2_resume")


@app.callback(
    Output("run-poll", "disabled", allow_duplicate=True),
    Output("scenario-poll", "disabled", allow_duplicate=True),
    Input("resync-check", "n_intervals"),
    prevent_initial_call=True,
)
def _resync_pollers(n_intervals):
    """Re-enables whichever poller(s) a genuinely in-progress run needs,
    once, right after every page load - see resync-check's own docstring
    (app.layout) for why this gap exists at all: _run_state/_scenario_state
    track a run's progress entirely server-side, completely independent of
    any browser session, but run-poll/scenario-poll's own "disabled" prop
    lives only in the browser and always resets to its layout default
    (True) on load - so a mid-run page refresh left the UI looking
    completely blank/stuck even though the actual solver kept computing
    normally the whole time (confirmed directly on a live decay run).

    scenario-poll drives the actual visible rendering for BOTH the sweep
    table and the unified tab's 1-combination case (see _poll_scenario) -
    re-enabled whenever EITHER state shows something active. run-poll
    additionally drives flow-decision-panel/phase2-resume-panel's own
    visibility (see _poll_run) - those are only ever toggled by run-poll,
    not scenario-poll, so a single-run decision pause specifically needs
    run-poll re-enabled too, not just scenario-poll.

    Does nothing (no_update) for whichever poller isn't actually needed -
    a normal launch already starts from a correctly-disabled state, and
    this must never turn a poller ON when nothing is running.
    """
    run_active = _run_state["status"] in _RUN_STATE_ACTIVE_STATUSES
    scenario_active = _scenario_state["status"] == "running"
    return (
        False if run_active else dash.no_update,
        False if (run_active or scenario_active) else dash.no_update,
    )


# Collapses DECAY_STEPS/STEADY_STATE_STEPS/CONTINUE_STEPS' more granular
# checklist step names (see _run_log's marker logic, which already tracks
# exactly which one is current) into one shared, simpler vocabulary for the
# Simulation Progress table's Status column - "Running" alone wasn't
# meaningful enough to tell setup/flow-convergence/the actual measurement
# apart at a glance.
_STAGE_LABEL_BY_STEP = {
    "Generate mesh": "Setup",
    "Write initial fields": "Setup",
    "Converge flow field": "Flow field calc",
    "Compute fluence & UV zones": "Setup",
    "Run pimpleFoam (decay)": "Decay sim",
    "Post-process & write results": "Post-processing",
    "Set up mesh, flow field, and UV zones": "Setup",
    "Carve contaminant source zone": "Setup",
    "Phase 1: source only (no UV)": "Phase 1",
    "Phase 2: source + UV": "Phase 2",
    "Write results": "Post-processing",
}


def _current_stage_label():
    """Human-meaningful phase name for a currently-running single run
    (Setup/Flow field calc/Decay sim/Phase 1/Phase 2/Post-processing),
    derived from _run_state's own step-tracking checklist rather than a
    bare "Running". Decay's concurrent UV-on+control pair share ONE step
    ("Run pimpleFoam (decay)") that's only marked done once BOTH threads
    finish (see _finish_decay's "Running postProcess volAverage" line,
    logged only after _run_decay_pair returns) - so this naturally shows
    "Decay sim" for as long as EITHER is still going, i.e. always the
    slower one, with no extra parallel-run tracking needed.
    """
    steps = _run_state.get("steps") or []
    step_status = _run_state.get("step_status", {})
    for s in steps:
        if step_status.get(s) == "running":
            return _STAGE_LABEL_BY_STEP.get(s, s)
    return "Running"


def _single_run_progress_table():
    """The Simulation Progress table's content for a 1-combination run
    (see _start_scenario_sweep) - _run_state has no "combos"/"results" the
    way _scenario_state does, so this builds the equivalent single row
    directly from _run_state instead of reusing _scenario_progress_table.
    """
    z = _run_state.get("z")
    ach = _run_state.get("ach")
    status = _run_state["status"]
    if status == "running":
        stage = _current_stage_label()
    else:
        stage = {
            "awaiting_decision": "paused - awaiting decision",
            "awaiting_phase2_resume": "paused - awaiting decision", "done": "Finished",
            "error": "error", "stopped": "Stopped",
        }.get(status, status)
    metrics = {"total_reduction_pct": None, "ach_efficiency_pct": None, "uv_efficiency_pct": None,
               "mechanical_mixing_efficiency_pct": None, "est_ach_per_hr": None, "est_each_per_hr": None}
    if status == "done" and _run_state.get("case_dir"):
        try:
            with open(f"{_run_state['case_dir']}/results.json") as f:
                metrics = combo_summary_metrics(migrate_result_keys(json.load(f)))  # pre-2026-09-02 files use the old key names
        except Exception:
            pass

    def _pct(v):
        return f"{v:.1f}%" if v is not None else ""

    def _rate(v):
        return f"{v:.4g} /hr" if v is not None else ""

    header = html.Tr([
        html.Th("Z"), html.Th("ACH"), html.Th("Status"), html.Th("Est. time to finish"),
        html.Th("Total reduction %"), html.Th("Measured ACH eff. %"), html.Th("Measured UV eff. %"),
        html.Th("Mechanical mixing eff. %"), html.Th("Est. ACH /hr"), html.Th("Est. eACH /hr"),
    ])
    est_time = _eta_text_from_progress(_run_state) if status == "running" else ""
    row = html.Tr([
        html.Td(z), html.Td(ach), html.Td(stage), html.Td(est_time),
        html.Td(_pct(metrics["total_reduction_pct"])), html.Td(_pct(metrics["ach_efficiency_pct"])),
        html.Td(_pct(metrics["uv_efficiency_pct"])), html.Td(_pct(metrics["mechanical_mixing_efficiency_pct"])),
        html.Td(_rate(metrics["est_ach_per_hr"])), html.Td(_rate(metrics["est_each_per_hr"])),
    ])
    return dbc.Table([header, row], bordered=False, hover=True, size="sm", className="small")


def _with_total_run_time(start_time, status_text):
    """Prepends a "Total run time: M:SS" line above the status text (see
    scenario-status-text) - computed fresh from start_time on every poll
    (every 2s, comfortably inside the user's own "update every 5 sec or
    so" ask) rather than a one-shot value, and left showing/still ticking
    at whatever elapsed time the run finished at once status is no longer
    "running" (start_time doesn't change, so this naturally freezes at the
    true total once nothing further updates it - see _poll_scenario's own
    poll-then-stop behavior once scenario-poll.disabled goes back to True).
    "" (no line at all) if start_time isn't set yet (nothing has ever run).
    """
    if not start_time:
        return status_text
    return [f"Total run time: {_format_mmss(time.time() - start_time)}", html.Br(), status_text]


@app.callback(
    Output("scenario-log", "children"),
    Output("scenario-live-status", "children"),
    Output("scenario-status-text", "children"),
    Output("scenario-progress-table", "children"),
    Output("scenario-poll", "disabled", allow_duplicate=True),
    Output("scenario-run-btn", "disabled", allow_duplicate=True),
    Output("scenario-stop-btn", "disabled", allow_duplicate=True),
    Input("scenario-poll", "n_intervals"),
    prevent_initial_call=True,
)
def _poll_scenario(n_intervals):
    # A 1-combination run (see _start_scenario_sweep) lives in _run_state,
    # not _scenario_state - render from whichever is actually active.
    # flow_decision_panel/phase2_resume_panel now live directly on THIS tab
    # (see scenario_tab) - their own visibility is toggled by _poll_run
    # (still firing, just invisible - see _processing_legacy), so this only
    # needs to cover the plain running/finished/stopped/error text while the
    # user stays on this tab watching it.
    #
    # The "was the last thing launched a 1-combo run?" check below must
    # compare start_time, not just "_run_state isn't idle" - _run_state is
    # never reset to idle by launching a genuine multi-combo sweep (only
    # len(combos)==1 ever touches it, see _start_scenario_sweep), so a
    # single test run from earlier in the session (left "done") would
    # otherwise permanently hijack this display the moment ANY later
    # multi-combo sweep finishes (_scenario_state["status"] leaving
    # "running" satisfies the rest of the condition) - confirmed live: a
    # finished multi-combo sweep's table collapsed down to a single
    # unrelated leftover row from an earlier single run.
    run_is_more_recent = (
        _run_state.get("start_time") is not None
        and (_scenario_state.get("start_time") is None
             or _run_state["start_time"] >= _scenario_state["start_time"])
    )
    if _run_state["status"] in _RUN_STATE_ACTIVE_STATUSES or (
            run_is_more_recent
            and _run_state["status"] != "idle" and _scenario_state["status"] != "running"
            and _run_state.get("case_dir") and _run_state.get("z") is not None):
        status = _run_state["status"]
        log_text = "\n".join(_run_state["log"][-300:])
        live_text = "\n".join(l for l in (_solver_progress_text(), _run_live_status_text()) if l) or "(nothing running)"
        still_running = status == "running"
        status_text = {
            "running": "Running... (1/1 combination)",
            "done": "Finished. 1/1 succeeded.",
            "error": "Failed - see log below.",
            "stopped": "Stopped.",
            "awaiting_decision": "Paused - awaiting your decision (see the panel above).",
            "awaiting_phase2_resume": "Paused - awaiting your decision (see the panel above).",
        }.get(status, "")
        status_text = _with_total_run_time(_run_state.get("start_time"), status_text)
        return (log_text, live_text, status_text, _single_run_progress_table(),
                not still_running, still_running, not still_running)

    status = _scenario_state["status"]
    log_text = "\n".join(_scenario_state["log"][-300:])
    live_status = _scenario_state.get("live_status", {})
    # live_status's dict KEY (e.g. "Z=6/ACH=3/UV-on") carries the actual
    # combo/phase identity - the stored value is just the raw "Time = N"
    # line (see _scenario_status_update), so the key must be rendered too
    # or the panel shows a bare "Time = N" with no way to tell which
    # concurrent combination it belongs to.
    live_text = "\n".join(f"[{k}] {live_status[k]}" for k in sorted(live_status)) or "(nothing running)"
    n_done = sum(1 for r in _scenario_state["results"].values() if r["status"] == "done")
    n_error = sum(1 for r in _scenario_state["results"].values() if r["status"] == "error")
    n_total = len(_scenario_state["combos"])
    still_running = status == "running"
    status_text = {
        "running": f"Running... ({n_done + n_error}/{n_total} combinations done)",
        "done": f"Finished. {n_done}/{n_total} succeeded, {n_error} failed.",
        "error": "Failed - see log below.",
        "stopped": f"Stopped. {n_done}/{n_total} succeeded, {n_error} failed.",
    }.get(status, "")
    status_text = _with_total_run_time(_scenario_state.get("start_time"), status_text)
    return (log_text, live_text, status_text, _scenario_progress_table(),
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


def _interleave_with_br(lines):
    """Dash children list joining each of `lines` (non-empty strings) with
    an html.Br() between them - "" for an empty list, so a caller can
    always just assign this straight to a Div's children without a
    separate empty-list special case.
    """
    if not lines:
        return ""
    out = [lines[0]]
    for l in lines[1:]:
        out.append(html.Br())
        out.append(l)
    return out


def _run_live_status_text():
    """"[stream] Time = N" lines from _run_state["live_status"] - the
    concurrent UV-off control run's own progress (decay mode's UV-on/
    control pair - see _run_decay_pair/_run_status_update), which
    _solver_progress_text()/_solver_eta_text() above don't cover (those
    only ever track the UV-on stream). "" when nothing else is
    concurrently running (every other run type - steady-state, flow
    convergence, Continue - only ever has one solver active at a time).
    """
    live = _run_state.get("live_status") or {}
    return "\n".join(f"[{k}] {live[k]}" for k in sorted(live))


def _flow_decision_iterations_suggestion(diagnostic):
    """A reasonable default for the "continue this many more iterations"
    input - enough additional chunks to reach the oscillation-acceptance
    check's own evidence requirement, or one more chunk if that requirement
    was already met (genuinely still growing, not just under-evidenced -
    see _oscillation_diagnostic) and the user wants to see if it settles.
    Just a starting suggestion, not a hard-coded answer - always editable.
    """
    chunk_size = diagnostic["chunk_size"]
    missing_chunks = diagnostic["chunks_needed_for_oscillation_check"] - diagnostic["chunks_available"]
    return max(chunk_size, missing_chunks * chunk_size)


def _phase1_decision_iterations_suggestion(diagnostic):
    """Same idea as _flow_decision_iterations_suggestion, but for a
    Phase1ExtrapolationUndecided pause - _phase1_extrapolation_diagnostic's
    dict has a completely different shape (no chunks_available/
    chunks_needed_for_oscillation_check/trend/last_chunk_rel_change - those
    are _oscillation_diagnostic-only keys), so the flow version's KeyError
    on this diagnostic went uncaught by the panel-rendering code below
    until a real Phase1ExtrapolationUndecided pause hit it live. Suggest
    enough chunks for a fresh full streak (chunk_size x streak_required) -
    the most generous reasonable starting point, always editable.
    """
    return diagnostic["chunk_size"] * diagnostic["streak_required"]


def _decision_panel_text(decision, diagnostic):
    """Builds the awaiting_decision panel's text for either pause kind -
    see _run_pipeline_thread/_handle_flow_convergence_undecided's `kind`.
    Flow convergence's diagnostic (_oscillation_diagnostic) and Phase 1
    extrapolation's (_phase1_extrapolation_diagnostic) have different
    shapes - only `summary`/`chunk_size`/`rel_tol` are common to both, so
    the flow-specific detail (chunks available/needed, trend, last chunk
    change) below only applies to kind == "flow".
    """
    header = f"Stopped after {decision['total_iterations']} iterations. {diagnostic['summary']}"
    if decision.get("kind") != "flow":
        return header
    return (
        header + "\n\n"
        f"Chunks available: {diagnostic['chunks_available']} "
        f"(need {diagnostic['chunks_needed_for_oscillation_check']} to check for a stable "
        f"oscillation) | trend: {diagnostic['trend']} | last chunk change: "
        + (f"{diagnostic['last_chunk_rel_change'] * 100:.3g}%"
           if diagnostic['last_chunk_rel_change'] is not None else "n/a")
        + f" (target <= {diagnostic['rel_tol'] * 100:.2g}%)"
    )


@app.callback(
    Output("run-log", "children"),
    Output("run-status-text", "children"),
    Output("run-btn", "disabled", allow_duplicate=True),
    Output("continue-btn", "disabled", allow_duplicate=True),
    Output("run-poll", "disabled"),
    Output("stop-btn", "disabled"),
    Output("pause-btn", "disabled"),
    Output("pause-btn", "children"),
    Output("run-checklist", "children"),
    Output("run-elapsed", "children"),
    Output("run-current-time", "children"),
    Output("results-data", "data", allow_duplicate=True),
    Output("results-case-dir", "data", allow_duplicate=True),
    Output("flow-decision-panel", "style"),
    Output("flow-decision-text", "children"),
    Output("flow-decision-iterations", "value"),
    Output("phase2-resume-panel", "style"),
    Output("phase2-resume-text", "children"),
    Input("run-poll", "n_intervals"),
    prevent_initial_call=True,
)
def _poll_run(n_intervals):
    status = _run_state["status"]
    log_text = "\n".join(_run_state["log"][-300:])
    status_text = {
        "running": "Running...",
        "done": "Finished.",
        # Deliberately NOT lumped in with "error" - this is an expected
        # pause awaiting a real choice, not a crash, and the whole point
        # of this status existing is that a user should never have to
        # wonder which one they're looking at (see FlowConvergenceUndecided).
        "awaiting_decision": "Paused - awaiting your decision (see panel above). Not an error, not hung.",
        "awaiting_phase2_resume": "Paused - an unfinished run is ready to resume (see panel above). "
                                   "Not an error, not hung.",
        "error": "Failed - see log below.",
        "stopped": "Stopped.",
    }.get(status, "")
    still_running = status == "running"
    paused = still_running and _run_state.get("pause_requested", False)
    if paused:
        status_text = "Paused (solver suspended in place - click Continue to resume, no iterations lost)."
    pause_btn_label = "Continue" if paused else "Pause"

    start = _run_state.get("start_time")
    elapsed = f"Elapsed: {_format_mmss(time.time() - start)}" if start else ""
    lines = [l for l in (_solver_progress_text(), _solver_eta_text(), _run_live_status_text()) if l]
    cur_time_text = lines[0] if len(lines) == 1 else _interleave_with_br(lines)

    # Auto-load this run's own results once it finishes, so the Analysis
    # tab has something to show without a separate manual step - polling
    # stops right after this (run-poll.disabled becomes True), so this
    # only fires once, exactly when status first becomes "done".
    results_data = dash.no_update
    results_case_dir = dash.no_update
    if status == "done" and _run_state.get("case_dir"):
        try:
            with open(f"{_run_state['case_dir']}/results.json") as f:
                results_data = migrate_result_keys(json.load(f))  # pre-2026-09-02 files use the old key names
            results_case_dir = _run_state["case_dir"]
        except Exception:
            results_data = dash.no_update

    decision = _run_state.get("decision")
    if status == "awaiting_decision" and decision:
        diagnostic = decision["diagnostic"]
        panel_style = {"display": "block"}
        panel_text = _decision_panel_text(decision, diagnostic)
        panel_iterations = (_flow_decision_iterations_suggestion(diagnostic) if decision.get("kind") == "flow"
                             else _phase1_decision_iterations_suggestion(diagnostic))
    else:
        panel_style = {"display": "none"}
        panel_text = dash.no_update
        panel_iterations = dash.no_update

    phase2_decision = _run_state.get("phase2_decision")
    if status == "awaiting_phase2_resume" and phase2_decision:
        resume_panel_style = {"display": "block"}
        resume_panel_text = phase2_decision["detail"]
    else:
        resume_panel_style = {"display": "none"}
        resume_panel_text = dash.no_update

    return (log_text, status_text, still_running, still_running, not still_running, not still_running,
            not still_running, pause_btn_label,
            _render_checklist(), elapsed, cur_time_text, results_data, results_case_dir,
            panel_style, panel_text, panel_iterations, resume_panel_style, resume_panel_text)


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
    # threaded=True: without it, Flask's dev server handles one HTTP
    # request at a time - with run-poll/scenario-poll firing every 2s
    # (plus any button click), a request that lands while the previous one
    # is still being handled (even briefly - a big progress table, a log
    # tail, a WSL status read) just queues, and the browser's fetch for it
    # can come back as "Callback failed: the server did not respond" even
    # though the app itself is fine and picks it up right after. _run_state/
    # _scenario_state are already read concurrently from background solver-
    # monitoring threads regardless of this setting, so handling requests
    # concurrently here doesn't introduce a new class of race condition.
    app.run(debug=True, use_reloader=False, threaded=True)
