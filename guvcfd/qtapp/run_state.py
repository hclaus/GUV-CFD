"""Background-thread run orchestration + shared state, mirroring
guvcfd.app's own _run_state/_run_log/_track_solver_time/_run_decay/
_run_steady_state design (a real run takes minutes, far too long for the
GUI thread, so it runs in a daemon thread while a QTimer polls this state
for the GUI to display) - reimplemented independently here (see
guvcfd/qtapp/__init__.py) rather than importing guvcfd.app.

Simplifications vs. the Dash app (flagged here, not hidden): single runs
only (no Z x ACH sweep UI yet); steady-state mode uses the nominal-ACH
ventilation baseline, not a dedicated UV-off control run (residence-
time-scaled deltaT IS wired up, via merge_project_deltat_settings/
resolve_phase_delta_ts below, same as the web app);
phase1_extrapolation_gate is hardcoded False for steady-state single
runs (see _finish_steady_state's own run_steady_state_scenario call), so
Phase1ExtrapolationUndecided can never actually be raised here - Phase 1
either runs to its configured budget or accepts extrapolation
automatically, never pausing for an interactive decision.

Continue/resume (2026-08-18): probe_resumable_state()/launch_continue()
let a stopped single run pick back up without re-meshing/re-converging
flow from scratch - see probe_resumable_state's own docstring for
exactly which stopped states are covered (flow convergence, and steady-
state's Phase 1/Phase 2 via setup_summary.json + steady_state_pipeline's
own checkpoint/pending tracking) and which aren't yet (a decay run
stopped before its own first completion - continue_decay only extends an
already-finished one further). Unlike Dash's equivalent
(_resume_pipeline_thread/_resume_phase1_extrapolation_thread), a
flow-convergence resume here is fully automatic (a sensible default
`additional_iterations`, no interactive continue-vs-accept decision
panel) - a deliberate, smaller-scope choice, not an oversight.

Stale-marker fix (2026-08-18): run_steady_state_scenario() reads Phase
1/2 checkpoint/pending markers UNCONDITIONALLY (see
steady_state_pipeline.clear_phase_resume_state's own docstring) - a
genuine fresh Start (mesh/0/ fields rebuilt from scratch) must clear
them first, or it silently resumes stale Phase 1/2 state against fields
it no longer matches, producing a confusing downstream solver IO error.
_run_steady_state and launch_continue's "flow" stage branch both call
clear_phase_resume_state() before handing off to _finish_steady_state();
launch_continue's "phases" stage branch deliberately does NOT, since
that path's entire purpose is to honor those same markers.
"""
import json
import re
import threading
import time
from pathlib import Path

from guv_calcs import Project

from .. import scenario_runs
from ..app_settings import load_advanced_settings, merge_project_openfoam_settings, capture_openfoam_settings
from ..case_io import read_latest_time_field, snapshot_openfoam_settings
from ..decay_analysis import (
    mechanical_mixing_efficiency_pct, spatial_coefficient_of_variation, write_results_summary,
)
from ..fan import fan_fvoptions_entry
from ..initial_fields import compute_inlet_velocities
from ..mesh_gen import opening_actual_area
from ..monitoring import splice_live_vol_average_if_needed
from ..monitoring_points import compute_monitoring_results
from ..run_pipeline import case_awaiting_flow_decision, resume_case_setup, setup_case
from ..splice import set_control_dict_time
from ..steady_state_pipeline import (
    REFERENCE_TARGET_T_SS, clear_phase_resume_state, merge_project_deltat_settings, resolve_phase_delta_ts,
    run_steady_state_scenario,
)
from ..ventilation_control import finish_ventilation_only_control, prepare_ventilation_only_control
from ..wsl_utils import StoppedByUser, run_wsl_or_raise, run_wsl_streaming, wsl_path
from . import helpers

_TIME_RE = re.compile(r"^Time\s*=\s*([\d.eE+-]+)\s*$")

# steady_state_pipeline._run_phase's own per-chunk narration line - X is
# this chunk's cumulative iteration start (1 for a phase's very first
# chunk), Z is the whole phase's own iteration budget - see
# RunState.begin_chunked_phases's docstring for the confirmed real
# confusion this closes (the live panel showing raw per-chunk progress
# reset to a small number every ~few hundred iterations, while the log's
# own narration correctly showed the true cumulative range).
_PHASE_CHUNK_RE = re.compile(r"Running simpleFoam \((\d+)-\d+ of (\d+) iterations, writing every")
# Logged unconditionally at the start of Phase 2's own code block (see
# RunState.log_fn's use of it) - the same substring _STAGE_MARKERS already
# watches for the Status column, reused here as a reliable phase-transition
# signal too.
_PHASE2_START_RE = re.compile(r"=== Phase 2: source \+ UV ===")

# Coarse "what stage is this run at right now" tracking for the Run tab's
# progress table Status column - guvcfd.app derives the same thing from a
# formal step/checklist system (_current_stage_label), which relies on
# scraping the same log narration this pipeline code already emits via
# log_fn either way. Simpler equivalent here: watch for the same narration
# substrings directly (setup_case/run_steady_state_scenario/_run_decay all
# emit these) and just overwrite RunState.stage as they arrive - correct by
# construction since log lines arrive in the same order the pipeline
# actually executes them, so list order below doesn't matter.
_STAGE_MARKERS = [
    ("Loading project", "Setup"), ("Writing mesh dicts", "Setup"), ("Running blockMesh", "Setup"),
    ("Converging flow field", "Flow field calc"),
    ("=== Phase 1", "Phase 1"), ("=== Phase 2", "Phase 2"),
    ("Preparing UV-off control", "Setup"), ("Running pimpleFoam", "Decay sim"),
    ("Running postProcess volAverage", "Post-processing"), ("Writing results summary", "Post-processing"),
    ("Post-processing UV-off control", "Post-processing"), ("Computing monitoring locations", "Post-processing"),
]


class RunState:
    """Plain, GUI-framework-independent run state - one instance drives one
    at-a-time run (single-run only, no concurrent sweeps yet - see module
    docstring). Every field here is read by the GUI thread's QTimer poll
    and written only from the background run thread (except stop_requested,
    set by the GUI's Stop button) - dict/attribute reads and writes are
    atomic enough under the GIL for this "eventually consistent, polled
    every ~500ms" use case, the same assumption guvcfd.app's own polling
    design already relies on.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.status = "idle"  # idle / running / done / error / stopped
        self.log = []
        self.case_dir = None
        self.sim_type = None
        self.current_time = None
        self.target_time = None
        self.phase_start_time = None
        self.start_time = None
        self._chunked_phases = []
        self._chunked_phase_idx = -1
        self.iteration_base = 0.0
        self.delta_t = 1.0
        self.live_status = {}
        self.error = None
        self.results = None
        self.stop_requested = False
        self.pause_requested = False
        self.stage = "Setup"

    # -- callbacks handed to the pipeline modules --

    def log_fn(self, msg):
        msg = str(msg)
        self.log.append(msg)
        if len(self.log) > 5000:
            del self.log[: len(self.log) - 5000]
        for marker, stage in _STAGE_MARKERS:
            if marker in msg:
                self.stage = stage
                break
        # "=== Phase 2: source + UV ===" is logged unconditionally right
        # when Phase 2's own code block starts - unlike inferring the
        # transition from a chunk's own chunk_start==1, this is reliable
        # regardless of HOW Phase 1 ended (a normal chunked finish, a
        # checkpoint-reuse skip with no chunk lines logged at all, or Phase
        # 2 itself resuming mid-chunk with chunk_start != 1 for its own
        # first logged chunk - all confirmed real cases, see
        # begin_chunked_phases's own docstring).
        if self._chunked_phase_idx == 0 and len(self._chunked_phases) > 1 and _PHASE2_START_RE.search(msg):
            self._chunked_phase_idx = 1
            n_iterations, delta_t = self._chunked_phases[1]
            self.target_time = n_iterations * delta_t
            self.phase_start_time = time.time()
            self.current_time = None
            self.delta_t = delta_t
        m = _PHASE_CHUNK_RE.search(msg)
        if m and 0 <= self._chunked_phase_idx < len(self._chunked_phases):
            self.iteration_base = int(m.group(1)) - 1

    def should_stop(self):
        return self.stop_requested

    def should_pause(self):
        return self.pause_requested

    def begin_phase(self, target_time):
        """Call right before launching a solver stage whose progress should
        be tracked - sets the "X of Y" / ETA basis. Unlike guvcfd.app
        (which infers phase targets by regex-scraping narration lines,
        since it doesn't control every call site directly), this app DOES
        control every call site, so it just sets this explicitly instead.

        For a single, unchunked stage only (decay's one pimpleFoam run to a
        fixed end_time - its own raw "Time = N" already IS the true
        cumulative simulation time). Steady-state's Phase 1/Phase 2 need
        begin_chunked_phases instead - see its own docstring.
        """
        self.target_time = target_time
        self.phase_start_time = time.time()
        self.current_time = None
        self.iteration_base = 0.0
        self.delta_t = 1.0

    def begin_chunked_phases(self, phases):
        """Call right before a sequence of one or more CHUNKED solver
        stages - e.g. [(phase1_iterations, phase1_delta_t),
        (phase2_iterations, phase2_delta_t)] for steady-state's Phase 1
        then Phase 2, both driven by one single, blocking
        run_steady_state_scenario() call with no opportunity to call
        begin_phase again in between.

        Unlike begin_phase's single fixed target, each phase here runs in
        ~few-hundred-iteration chunks that each restart OpenFOAM's own
        "Time" counter at 0 (see steady_state_pipeline._run_phase's own
        docstring) - confirmed as a real, live point of confusion: the
        "RUNNING NOW" panel showed "Time step 130 of 12000" while the log's
        own narration said the current chunk was "4401-4800 of 12000",
        because solver_log_fn used to store each chunk's raw, un-offset
        Time directly. log_fn above now watches for _PHASE_CHUNK_RE (the
        exact narration line _run_phase emits at the start of every chunk)
        to keep iteration_base current, and separately for _PHASE2_START_RE
        to detect the Phase 1 -> Phase 2 transition - solver_log_fn below
        adds iteration_base back onto every raw "Time = N" line, converted
        to OpenFOAM Time units via this phase's own delta_t (iterations and
        Time aren't the same units whenever residence-time deltaT scaling
        is active).
        """
        self._chunked_phases = list(phases)
        self._chunked_phase_idx = 0 if phases else -1
        self.iteration_base = 0.0
        if phases:
            n_iterations, delta_t = phases[0]
            self.target_time = n_iterations * delta_t
            self.delta_t = delta_t
        else:
            self.target_time = None
            self.delta_t = 1.0
        self.phase_start_time = time.time()
        self.current_time = None

    def solver_log_fn(self, line):
        """on_line callback for a solver's raw stdout - updates current_time
        from "Time = N" lines without appending anything to the log (an
        OpenFOAM run prints many lines per timestep; forwarding all of it
        would flood the log fast enough to scroll real narration out of
        view within seconds - see guvcfd.app._track_solver_time).

        iteration_base/delta_t (0.0/1.0 unless begin_chunked_phases has set
        them - see its own docstring) convert a chunk-local raw Time back
        into the true cumulative value across every chunk so far.
        """
        stripped = line.strip()
        if stripped.startswith("["):
            self.log_fn(stripped)
            return
        m = _TIME_RE.match(stripped)
        if m:
            self.current_time = str(float(m.group(1)) + self.iteration_base * self.delta_t)

    def live_status_fn(self, key, msg):
        """Per-stream "latest Time = N", overwritten in place - used for
        decay mode's concurrent UV-on + UV-off-control pair (see
        _run_decay_pair below), same design as guvcfd.app's live_status.
        """
        if msg is None:
            self.live_status.pop(key, None)
        else:
            self.live_status[key] = msg

    def progress_text(self):
        cur = self.current_time
        if not cur:
            return ""
        try:
            cur_val = float(cur)
        except (TypeError, ValueError):
            return f"Simulation time step {cur}"
        target = self.target_time
        if not target:
            return f"Simulation time step {cur_val:.4g}"
        pct = min(100, round(100 * cur_val / target))
        return f"Simulation time step {cur_val:.4g} of {target:.4g} ({pct}%)"

    def eta_text(self):
        cur, target, phase_start = self.current_time, self.target_time, self.phase_start_time
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
        remaining = (target - cur_val) / rate
        m, s = divmod(max(0, int(remaining)), 60)
        return f"Expected finish of this step in {m}:{s:02d}"

    def live_status_text(self):
        return "\n".join(f"[{k}] {self.live_status[k]}" for k in sorted(self.live_status))


def launch_run(state, guv_path, case_dir, room, settings):
    """Start a full setup+solve run on a background daemon thread. state
    must be idle - caller (the Run tab) is responsible for checking that
    and disabling the Run button meanwhile.
    """
    state.reset()
    state.status = "running"
    state.case_dir = case_dir
    state.sim_type = settings["sim-type"]
    state.start_time = time.time()

    def worker():
        try:
            if settings["sim-type"] == "decay":
                _run_decay(state, guv_path, case_dir, room, settings)
            else:
                _run_steady_state(state, guv_path, case_dir, room, settings)
            state.status = "stopped" if state.stop_requested else "done"
        except StoppedByUser:
            state.status = "stopped"
        except Exception as e:
            state.error = str(e)
            state.status = "error"
            state.log_fn(f"ERROR: {e}")

    threading.Thread(target=worker, daemon=True).start()


def launch_continue(state, guv_path, case_dir, room, settings):
    """Resume a stopped single run on a background daemon thread, instead
    of launch_run's full setup+solve from scratch - dispatches to the
    right resume mechanism based on probe_resumable_state()'s own disk
    inspection (works even after an app restart, no in-memory record of
    the original stop needed). state must be idle, same caller contract
    as launch_run - the caller (Run tab) is responsible for only enabling
    this when probe_resumable_state() actually found something.
    """
    state.reset()
    state.status = "running"
    state.case_dir = case_dir
    state.sim_type = settings["sim-type"]
    state.start_time = time.time()

    def worker():
        try:
            adv = merge_project_openfoam_settings(settings, load_advanced_settings())
            probe = probe_resumable_state(case_dir, settings["sim-type"])
            if probe is None:
                raise RuntimeError("Nothing to resume in this case directory - it may already be "
                                    "finished, or never started.")
            mismatches = _settings_mismatch(case_dir, settings)
            if mismatches:
                details = "; ".join(f"{f}: was {p!r}, now {c!r}" for f, p, c in mismatches)
                raise RuntimeError(
                    f"Can't resume - current settings differ from what this case's mesh/flow field "
                    f"were actually built with ({details}). Revert the change, or Start fresh instead "
                    f"of Continue.")
            if probe["stage"] == "flow":
                additional = max(adv["flow-max-iterations"] - probe["total_iterations"], 500)
                state.log_fn(f"=== Resuming flow convergence ({probe['total_iterations']} iterations so "
                             f"far, {additional} more) ===")
                summary = _resume_case_setup_common(state, guv_path, case_dir, room, settings, adv, additional)
                if state.should_stop():
                    raise StoppedByUser("Stopped after resuming case setup.")
                if settings["sim-type"] == "decay":
                    _finish_decay(state, case_dir, room, settings, summary)
                else:
                    # "flow" stage means the earlier attempt stopped before
                    # Phase 1 ever started (0/fluenceRate didn't exist yet) -
                    # no legitimate Phase 1/2 progress can exist for this
                    # case_dir. Clear defensively, same reasoning as the
                    # fresh-Start path in _run_steady_state, in case this
                    # case_dir was previously used by an older attempt.
                    clear_phase_resume_state(case_dir)
                    _finish_steady_state(state, case_dir, room, settings, summary)
            elif probe["stage"] == "phases" and probe["resumable"]:
                state.log_fn("=== Resuming from setup already on disk (mesh/flow field/UV zones "
                             "unchanged) ===")
                summary = _read_setup_summary(case_dir)
                _finish_steady_state(state, case_dir, room, settings, summary)
            else:
                raise RuntimeError(probe.get("reason") or "Nothing to resume in this case directory.")
            state.status = "stopped" if state.stop_requested else "done"
        except StoppedByUser:
            state.status = "stopped"
        except Exception as e:
            state.error = str(e)
            state.status = "error"
            state.log_fn(f"ERROR: {e}")

    threading.Thread(target=worker, daemon=True).start()


# Ported from guvcfd.app's identical list (2026-08-18) - fields that
# change the mesh itself, or the converged flow field's own boundary
# values, so resuming with a DIFFERENT current value than what's
# actually built on disk would silently apply a mismatched setting to a
# case that was never (re)built for it. Monitoring points/source_center
# are deliberately excluded, same as app.py - pure post-processing
# positions, not mesh/flow-affecting.
_MESH_AFFECTING_FIELDS = [
    "ach", "z-value",
    "inlet-wall", "inlet-y-input", "inlet-z-input", "inlet-size-w", "inlet-size-h",
    "inlet-diffuser-type",
    "outlet-wall", "outlet-y-input", "outlet-z-input", "outlet-size-w", "outlet-size-h",
    "inlet2-enable", "inlet2-wall", "inlet2-y-input", "inlet2-z-input",
    "inlet2-size-w", "inlet2-size-h", "inlet2-diffuser-type",
    "outlet2-enable", "outlet2-wall", "outlet2-y-input", "outlet2-z-input",
    "outlet2-size-w", "outlet2-size-h",
    "fan-enable", "fan-speed", "fan-direction", "fan-radius", "fan-thickness",
    "fan-x-input", "fan-y-input", "fan-z-input",
]


def _settings_mismatch(case_dir, current_settings):
    """Compare current settings against what case_dir's mesh/flow field
    were actually last built with (run_settings.json, written by
    _save_run_settings before setup_case() runs). Returns a list of
    (field, prior_value, current_value) tuples for anything that differs
    among _MESH_AFFECTING_FIELDS; [] if nothing differs or there's no
    prior record to compare against. launch_continue refuses to resume
    when this is non-empty, rather than silently applying a changed
    setting to a case built under a different one.
    """
    path = f"{case_dir}/run_settings.json"
    if not Path(path).exists():
        return []
    with open(path) as f:
        prior = json.load(f)
    return [(field, prior[field], current_settings.get(field))
            for field in _MESH_AFFECTING_FIELDS
            if field in prior and prior[field] != current_settings.get(field)]


def _save_run_settings(case_dir, settings, guv_path):
    data = dict(settings)
    data["guv_path"] = guv_path
    if settings.get("sim-type") == "steady_state":
        data["source_center"] = (settings.get("inject-x-input"), settings.get("inject-y-input"),
                                  settings.get("inject-z-input"))
    with open(f"{case_dir}/run_settings.json", "w") as f:
        json.dump(data, f, indent=2)


def _setup_case_common(state, guv_path, case_dir, room, settings, adv):
    return setup_case(
        guv_path, case_dir, template_case_dir=helpers.TEMPLATE_CASE_DIR,
        Z=settings["z-value"], ach=settings["ach"],
        inlet_wall=settings["inlet-wall"],
        inlet_center=helpers.opening_center_frac(settings, "inlet", room),
        inlet_size=(settings["inlet-size-w"], settings["inlet-size-h"]),
        inlet_diffuser_type=settings.get("inlet-diffuser-type", "direct"),
        outlet_wall=settings["outlet-wall"],
        outlet_center=helpers.opening_center_frac(settings, "outlet", room),
        outlet_size=(settings["outlet-size-w"], settings["outlet-size-h"]),
        cell_size=adv["mesh-cell-size"], nbins=adv["uv-zone-bins"],
        flow_rel_tol=adv["flow-rel-tol"] / 100.0, flow_max_iterations=adv["flow-max-iterations"],
        oscillation_window=adv["oscillation-window"], oscillation_growth_tol=adv["oscillation-growth-tol"],
        ach_delivery_tol=adv["ach-delivery-tol"] / 100.0,
        momentum_relaxation=adv["momentum-relaxation"], scalar_relaxation=adv["scalar-relaxation"],
        scalar_transport_ncorr=adv["scalar-transport-ncorr"],
        scalar_transport_tolerance=adv["scalar-transport-tolerance"],
        pimple_end_time=settings.get("pimple-end-time", 120),
        pimple_write_interval=settings.get("pimple-write-interval", 10),
        pimple_delta_t=adv["pimple-delta-t"], max_co=adv["max-co"],
        log_fn=state.log_fn, should_stop=state.should_stop, solver_log_fn=state.solver_log_fn,
        should_pause=state.should_pause,
        sealed=settings["ach"] <= 0, mechanical_ach_only=bool(settings.get("mech-ach-only")),
        **helpers.fan_kwargs(settings),
        **helpers.second_opening_kwargs(settings, "inlet2", room),
        **helpers.second_opening_kwargs(settings, "outlet2", room),
    )


def _resume_case_setup_common(state, guv_path, case_dir, room, settings, adv, additional_iterations):
    """resume_case_setup()'s equivalent of _setup_case_common - reuses the
    existing mesh/0/ fields/fvOptions on disk exactly as an earlier,
    stopped attempt left them, running `additional_iterations` more flow-
    convergence iterations before finishing the rest of setup (fluenceRate,
    cellZones, fvOptions) exactly like a fresh setup_case() call would.

    Deliberately NOT a **kwargs reuse of helpers.fan_kwargs/
    second_opening_kwargs (unlike _setup_case_common above) -
    resume_case_setup's own parameter surface is narrower by design (see
    its docstring: mesh/BC values are already on disk, unchanged; only
    what's needed to resume the SOLVE and finish setup's remaining
    bookkeeping is accepted) - fan_center/fan_disk_radius/
    fan_disk_thickness and outlet2_center/outlet2_size aren't parameters
    there at all, so splatting those helpers' full dicts would raise.
    """
    has_inlet2 = bool(settings.get("inlet2-enable"))
    has_outlet2 = bool(settings.get("outlet2-enable"))
    fan_direction = (0, 0, -1) if settings.get("fan-direction", "down") == "down" else (0, 0, 1)
    return resume_case_setup(
        case_dir, guv_path, decision="continue", ach=settings["ach"], Z=settings["z-value"],
        nbins=adv["uv-zone-bins"],
        inlet_wall=settings["inlet-wall"],
        inlet_center=helpers.opening_center_frac(settings, "inlet", room),
        inlet_size=(settings["inlet-size-w"], settings["inlet-size-h"]),
        inlet_diffuser_type=settings.get("inlet-diffuser-type", "direct"),
        inlet2_wall=settings["inlet2-wall"] if has_inlet2 else None,
        inlet2_center=helpers.opening_center_frac(settings, "inlet2", room) if has_inlet2 else None,
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
        fan_speed=settings.get("fan-speed") if settings.get("fan-enable") else None,
        fan_direction=fan_direction,
        mechanical_ach_only=bool(settings.get("mech-ach-only")),
        log_fn=state.log_fn, should_stop=state.should_stop, solver_log_fn=state.solver_log_fn,
        should_pause=state.should_pause,
    )


def _run_decay(state, guv_path, case_dir, room, settings):
    adv = merge_project_openfoam_settings(settings, load_advanced_settings())
    capture_openfoam_settings(settings, adv)
    _save_run_settings(case_dir, settings, guv_path)
    state.log_fn("=== Setting up mesh, flow field, and UV zones ===")
    summary = _setup_case_common(state, guv_path, case_dir, room, settings, adv)
    if state.should_stop():
        raise StoppedByUser("Stopped after case setup.")
    _finish_decay(state, case_dir, room, settings, summary)


def _finish_decay(state, case_dir, room, settings, summary):
    """Everything _run_decay does after setup_case() returns a summary -
    factored out so launch_continue's "flow" resume path (setup_case()
    itself was interrupted mid-flow-convergence, resumed via
    resume_case_setup) can reach the exact same steps afterward, matching
    _finish_steady_state's identical split. Decay has no equivalent
    resume for a stop AFTER this point yet (see probe_resumable_state's
    own docstring) - this split only serves the "flow" stage.
    """
    adv = merge_project_openfoam_settings(settings, load_advanced_settings())
    sealed = settings["ach"] <= 0
    mech_ach_only = bool(settings.get("mech-ach-only"))
    skip_control = sealed or mech_ach_only
    case_dir_wsl = wsl_path(case_dir)
    control_dir = f"{case_dir}/no_UV"
    control_dir_wsl = wsl_path(control_dir)

    combined_end_time, control_end_time = helpers.decay_run_durations(
        settings["ach"], summary["eACH_uv_well_mixed_mean"], adv)
    write_interval = max(1, settings.get("pimple-write-interval", 10))
    set_control_dict_time(case_dir, end_time=combined_end_time,
                           write_interval=write_interval, delta_t=adv["pimple-delta-t"], max_co=adv["max-co"])
    splice_live_vol_average_if_needed(case_dir)

    if state.should_stop():
        raise StoppedByUser("Stopped before pimpleFoam.")
    if sealed:
        state.log_fn("Sealed room: skipping the UV-off control run - ventilation-only decay "
                      "rate is exactly 0 by construction (no opening for mass to leave through).")
    elif mech_ach_only:
        state.log_fn("Mechanical ACH only: skipping the UV-off control run - this run's own decay "
                     "curve (empty UV source) already IS the ventilation-only measurement.")
    else:
        state.log_fn('=== Preparing UV-off control ("no_UV") ===')
        has_inlet2 = bool(settings.get("inlet2-enable"))
        prepare_ventilation_only_control(
            case_dir, control_dir, summary["inlet_velocity"],
            control_end_time, write_interval, pimple_delta_t=adv["pimple-delta-t"], max_co=adv["max-co"],
            inlet2_velocity=summary.get("inlet2_velocity") if has_inlet2 else None,
            has_outlet2=bool(settings.get("outlet2-enable")),
            sealed=False, log_fn=state.log_fn, should_stop=state.should_stop,
        )

    state.log_fn(f"Running pimpleFoam: UV-on ({combined_end_time}s)"
                 + ("" if skip_control else f" + UV-off control ({control_end_time}s)") + "...")
    state.begin_phase(combined_end_time)
    r_uv, r_control = _run_decay_pair(
        state, case_dir_wsl, None if skip_control else control_dir_wsl,
        combined_end_time=combined_end_time, control_end_time=None if skip_control else control_end_time)
    if state.should_stop():
        raise StoppedByUser("Stopped during pimpleFoam.")
    runs = [("UV-on", r_uv)] if skip_control else [("UV-on", r_uv), ("UV-off control", r_control)]
    for label, r in runs:
        if r.returncode != 0 or "FOAM FATAL" in r.stdout or "Floating Point Exception" in r.stdout:
            tail = "\n".join(r.stdout.splitlines()[-25:]) or "(no output captured)"
            raise RuntimeError(f"{label} pimpleFoam failed (exit {r.returncode}):\n{tail}")

    try:
        spatial_cov = spatial_coefficient_of_variation(read_latest_time_field(case_dir, "T"))
    except Exception:
        spatial_cov = None

    if mech_ach_only:
        state.log_fn("Writing results summary (mechanical ACH only - no separate control run to "
                     "correct against; total_ach_effective is this run's own measured rate)...")
        results = write_results_summary(
            case_dir, f"{case_dir}/results.json", settings["ach"], 0.0,
            extra={
                "n_lamps": summary["n_lamps"], "fluence_mean": summary["fluence_mean"],
                "flow_converged": summary.get("flow_converged"), "ach_delivery": summary.get("ach_delivery"),
                "spatial_cov_final": spatial_cov,
            },
        )
    else:
        if sealed:
            control_results = {"total_ach_effective": 0.0}
        else:
            state.log_fn("=== Post-processing UV-off control ===")
            control_results = finish_ventilation_only_control(control_dir, settings["ach"], log_fn=state.log_fn)

        state.log_fn("Writing results summary...")
        results = write_results_summary(
            case_dir, f"{case_dir}/results.json", settings["ach"], summary["eACH_uv_well_mixed_mean"],
            extra={
                "n_lamps": summary["n_lamps"], "fluence_mean": summary["fluence_mean"],
                "flow_converged": summary.get("flow_converged"), "ach_delivery": summary.get("ach_delivery"),
                "spatial_cov_final": spatial_cov,
            },
            measured_ventilation_ach=control_results["total_ach_effective"],
            measured_ventilation_ach_ci95=control_results.get("total_ach_effective_ci95"),
            measured_ventilation_ach_se_per_s=control_results.get("fit_se_per_s"),
            measured_ventilation_fit_dof=(control_results["fit_n"] - 2) if control_results.get("fit_n") else None,
        )

    points = helpers.gather_monitoring_points(settings)
    if points:
        results["monitoring"] = compute_monitoring_results(
            case_dir, points, cell_size=adv["mesh-cell-size"], ventilation_ach=settings["ach"], log_fn=state.log_fn)
        with open(f"{case_dir}/results.json", "w") as f:
            json.dump(results, f, indent=2)

    state.results = results
    state.log_fn(f"Done. eACH_uv effective={results['eACH_uv_effective']:.4g} /hr "
                 f"(well-mixed={results['eACH_uv_well_mixed']:.4g} /hr)")


def _run_decay_pair(state, case_dir_wsl, control_dir_wsl, combined_end_time=None, control_end_time=None):
    """Run UV-on (and, unless control_dir_wsl is None, the UV-off control)
    pimpleFoam solves concurrently - see guvcfd.app._run_decay_pair for the
    identical design this mirrors."""
    results = {}
    errors = {}

    def run_one(name, cwd_wsl, on_line, log_prefix, total_time):
        try:
            callback = scenario_runs._throttled_solver_callback(
                state.log_fn, log_prefix, on_line=on_line,
                status_fn=state.live_status_fn, status_key=log_prefix, total_time=total_time)
            results[name] = run_wsl_streaming(
                "pimpleFoam 2>&1 | tee log.pimpleFoam", cwd_wsl,
                on_line=callback, should_stop=state.should_stop, kill_pattern="pimpleFoam",
                should_pause=state.should_pause,
            )
        except Exception as e:
            errors[name] = e
        finally:
            state.live_status_fn(log_prefix, None)

    threads = [threading.Thread(target=run_one,
                                 args=("uv", case_dir_wsl, state.solver_log_fn, "UV-on", combined_end_time))]
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


def _write_setup_summary(case_dir, summary):
    """Persist setup_case()'s return value (fluence_mean,
    eACH_uv_well_mixed_mean, flow_converged, ach_delivery, etc.) right
    after it's computed, so a steady-state run stopped anywhere inside
    run_steady_state_scenario() (Phase 1, Phase 2, or the bookkeeping
    between them) can be resumed later by re-entering _finish_steady_state
    WITHOUT re-running setup_case() - which would redo mesh generation and
    flow convergence from scratch, discarding the very state the stop
    happened downstream of. Ported from guvcfd.app's identical function
    (2026-08-18) - this app never had it, which is exactly why "Continue"
    for a stopped steady-state run had no way to skip setup before today.
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


def probe_resumable_state(case_dir, sim_type):
    """Inspect case_dir on disk (no in-memory knowledge of how/whether a
    previous attempt stopped needed - works from a fresh app restart too,
    matching run_pipeline.case_awaiting_flow_decision's own design) and
    report whether/how "Continue" can resume it.

    Returns None if there's nothing to resume (case_dir doesn't exist or
    is empty, or the run already finished normally - results.json
    exists). Otherwise a dict: {"stage": "flow", "total_iterations": N}
    (flow convergence has chunk history but never reached a verdict - see
    run_pipeline.case_awaiting_flow_decision) or {"stage": "phases",
    "resumable": bool, "reason": str} (setup_case() fully finished -
    0/fluenceRate exists - but the scenario itself didn't).

    "phases" is resumable for steady-state (run_steady_state_scenario
    already auto-detects/resumes Phase 1 via its own checkpoint/pending
    and, since today's Phase 2 pending-progress addition, Phase 2 too -
    see steady_state_pipeline._write_phase2_pending). It is NOT resumable
    for decay yet - confirmed while building this: decay's own
    continue_decay requires an already-COMPLETE results.json (it EXTENDS
    a finished run further, a different feature from resuming one
    interrupted mid-transient), and there is no decay equivalent of
    setup_summary.json/phase-pending tracking in either app today. A
    decay run stopped before ever completing once has no resume path
    yet - "phases" is reported with resumable=False and a reason for
    that case, rather than silently mishandling it.
    """
    if not case_dir or not Path(case_dir).exists():
        return None
    if Path(f"{case_dir}/results.json").exists():
        return None
    if Path(f"{case_dir}/0/fluenceRate").exists():
        if sim_type == "decay":
            return {"stage": "phases", "resumable": False,
                    "reason": "Resuming a decay run stopped before it ever completed once isn't supported "
                              "yet - only extending an already-finished run further is (Extend / modify "
                              "simulations...)."}
        return {"stage": "phases", "resumable": True, "reason": ""}
    pending = case_awaiting_flow_decision(case_dir)
    if pending is not None:
        return {"stage": "flow", "total_iterations": pending["total_iterations"]}
    return None


def _run_steady_state(state, guv_path, case_dir, room, settings):
    if settings["ach"] <= 0:
        raise ValueError("Sealed-room / ACH<=0 is only supported in Decay mode.")
    adv = merge_project_openfoam_settings(settings, load_advanced_settings())
    capture_openfoam_settings(settings, adv)
    _save_run_settings(case_dir, settings, guv_path)

    state.log_fn("=== Setting up mesh, flow field, and UV zones ===")
    # A genuinely fresh Start rebuilds the mesh/0/ fields from scratch -
    # any Phase 1/2 checkpoint/pending marker left over from an earlier,
    # stopped attempt at this same case_dir must not survive it, or
    # run_steady_state_scenario()'s own unconditional checkpoint/pending
    # detection will silently resume against fields that no longer match.
    clear_phase_resume_state(case_dir)
    summary = _setup_case_common(state, guv_path, case_dir, room, settings, adv)
    if state.should_stop():
        raise StoppedByUser("Stopped after case setup.")
    _finish_steady_state(state, case_dir, room, settings, summary)


def _finish_steady_state(state, case_dir, room, settings, summary):
    """Everything _run_steady_state does after setup_case() returns a
    summary - factored out (mirroring guvcfd.app's identical split) so
    launch_continue's "phases" resume path can reach the exact same steps
    using a setup_summary.json persisted by an earlier, stopped attempt,
    skipping setup_case() (mesh generation + flow convergence) entirely.
    """
    adv = merge_project_openfoam_settings(settings, load_advanced_settings())
    # Persisted here (not just held in this call's summary argument) so a
    # stop anywhere below - inside run_steady_state_scenario() or in the
    # results.json bookkeeping after it - can be resumed via
    # probe_resumable_state()/launch_continue(), without re-running
    # setup_case()'s mesh generation and flow convergence from scratch.
    _write_setup_summary(case_dir, summary)

    fan_entry = None
    if settings.get("fan-enable"):
        direction = (0, 0, -1) if settings.get("fan-direction", "down") == "down" else (0, 0, 1)
        fan_entry = fan_fvoptions_entry(settings["fan-speed"], direction=direction)

    room_volume = room.x * room.y * room.z
    cell_size = adv["mesh-cell-size"]
    has_inlet2 = bool(settings.get("inlet2-enable"))
    has_outlet2 = bool(settings.get("outlet2-enable"))
    openings = [(settings["inlet-wall"],
                 opening_actual_area(settings["inlet-wall"], room.x, room.y, room.z,
                                      helpers.opening_center_frac(settings, "inlet", room),
                                      (settings["inlet-size-w"], settings["inlet-size-h"]), cell_size))]
    if has_inlet2:
        openings.append((settings["inlet2-wall"],
                          opening_actual_area(settings["inlet2-wall"], room.x, room.y, room.z,
                                               helpers.opening_center_frac(settings, "inlet2", room),
                                               (settings["inlet2-size-w"], settings["inlet2-size-h"]), cell_size)))
    velocities = compute_inlet_velocities(settings["ach"], room_volume, openings)
    inlet_velocity = velocities[0]
    inlet2_velocity = velocities[1] if has_inlet2 else None

    phase1_iterations = settings.get("phase1-iterations", 4000)
    phase2_iterations = settings.get("phase2-iterations", 2000)
    eACH_uv_well_mixed = summary.get("eACH_uv_well_mixed_mean", 0.0)
    deltat_adv = merge_project_deltat_settings(settings, adv)
    phase1_delta_t, phase2_delta_t = resolve_phase_delta_ts(
        settings["ach"], eACH_uv_well_mixed, phase1_iterations, phase2_iterations, deltat_adv)
    if phase1_delta_t != 1 or phase2_delta_t != 1:
        state.log_fn(f"Residence-time-scaled deltaT: phase1={phase1_delta_t}, phase2={phase2_delta_t}.")

    state.log_fn(f"Running Phase 1 (no UV, {phase1_iterations} iterations) then "
                 f"Phase 2 (+UV, {phase2_iterations} iterations)...")
    state.begin_chunked_phases([(phase1_iterations, phase1_delta_t), (phase2_iterations, phase2_delta_t)])
    result = run_steady_state_scenario(
        case_dir, room.x, room.y, room.z, settings["ach"], settings["z-value"],
        source_center=(settings["inject-x-input"], settings["inject-y-input"], settings["inject-z-input"]),
        target_T_ss=REFERENCE_TARGET_T_SS,
        inlet_velocity=inlet_velocity, inlet2_velocity=inlet2_velocity, has_outlet2=has_outlet2,
        inlet_diffuser_type=settings.get("inlet-diffuser-type", "direct"),
        inlet_wall=settings["inlet-wall"], inlet_center=helpers.opening_center_frac(settings, "inlet", room),
        inlet_size=(settings["inlet-size-w"], settings["inlet-size-h"]),
        inlet2_diffuser_type=settings.get("inlet2-diffuser-type", "direct") if has_inlet2 else "direct",
        inlet2_wall=settings["inlet2-wall"] if has_inlet2 else None,
        inlet2_center=helpers.opening_center_frac(settings, "inlet2", room) if has_inlet2 else None,
        inlet2_size=(settings["inlet2-size-w"], settings["inlet2-size-h"]) if has_inlet2 else None,
        phase1_iterations=phase1_iterations, phase2_iterations=phase2_iterations,
        phase1_write_interval=adv["phase-write-interval"], phase2_write_interval=adv["phase-write-interval"],
        window_frac=settings.get("t-ss-window-frac") or 0.15,
        cell_size=adv["mesh-cell-size"], nbins=adv["uv-zone-bins"],
        source_size=settings["source-zone-size"],
        plateau_rel_tol=adv["plateau-rel-tol"] / 100.0, mass_balance_tol=adv["mass-balance-tol"] / 100.0,
        t_inf_check_interval=adv["phase-chunk-size"] if adv["t-infinity-early-stop-enabled"] else None,
        t_inf_rel_tol=(adv["t-infinity-rel-tol"] / 100.0) if adv["t-infinity-early-stop-enabled"] else None,
        t_inf_streak=adv["phase1-extrapolation-streak"],
        keep_all_timesteps=adv["keep-all-timesteps"], phase1_t_initial=adv["phase1-t-initial"],
        phase1_extrapolation_gate=False,  # simplification - see module docstring
        fan_entry=fan_entry, monitoring_points=helpers.gather_monitoring_points(settings),
        patches_to_monitor=("outlet", "outlet2") if has_outlet2 else ("outlet",),
        log_fn=state.log_fn, should_stop=state.should_stop, solver_log_fn=state.solver_log_fn,
        should_pause=state.should_pause,
        phase1_delta_t=phase1_delta_t, phase2_delta_t=phase2_delta_t,
    )
    result["fluence_mean"] = summary["fluence_mean"]
    result["eACH_uv_well_mixed"] = summary.get("eACH_uv_well_mixed_mean")
    result["flow_converged"] = summary.get("flow_converged")
    result["ach_delivery"] = summary.get("ach_delivery")
    result["mechanical_mixing_efficiency_pct"] = mechanical_mixing_efficiency_pct(result)
    try:
        snapshot_openfoam_settings(case_dir)
    except Exception:
        pass  # archival only - never block a results.json write over it
    with open(f"{case_dir}/results.json", "w") as f:
        json.dump(result, f, indent=2)
    state.results = result
    # Finished end-to-end - nothing left to resume.
    _clear_setup_summary(case_dir)
    state.log_fn(f"Done. Reduction={result['reduction_pct']:.1f}%, "
                 f"eACH_uv={result['eACH_uv_steady_state']:.4g} /hr")
