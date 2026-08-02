"""Background-thread run orchestration + shared state, mirroring
guvcfd.app's own _run_state/_run_log/_track_solver_time/_run_decay/
_run_steady_state design (a real run takes minutes, far too long for the
GUI thread, so it runs in a daemon thread while a QTimer polls this state
for the GUI to display) - reimplemented independently here (see
guvcfd/qtapp/__init__.py) rather than importing guvcfd.app.

Simplifications vs. the Dash app (flagged here, not hidden): single runs
only (no Z x ACH sweep UI yet); no flow-convergence-undecided / Phase-1-
extrapolation-undecided resume UX (a run that hits one of those pauses
surfaces it as an error in the log instead of an interactive decision
panel - rerun, or use the web app to resume it); steady-state mode uses
the nominal-ACH ventilation baseline, not a dedicated UV-off control run
(residence-time-scaled deltaT IS wired up, via merge_project_deltat_settings/
resolve_phase_delta_ts below, same as the web app).
"""
import json
import re
import threading
import time
from pathlib import Path

from guv_calcs import Project

from .. import scenario_runs
from ..app_settings import load_advanced_settings
from ..case_io import read_latest_time_field
from ..decay_analysis import (
    mechanical_mixing_efficiency_pct, spatial_coefficient_of_variation, write_results_summary,
)
from ..fan import fan_fvoptions_entry
from ..initial_fields import compute_inlet_velocities
from ..monitoring_points import compute_monitoring_results
from ..run_pipeline import setup_case
from ..splice import set_control_dict_time
from ..steady_state_pipeline import (
    REFERENCE_TARGET_T_SS, merge_project_deltat_settings, resolve_phase_delta_ts, run_steady_state_scenario,
)
from ..ventilation_control import finish_ventilation_only_control, prepare_ventilation_only_control
from ..wsl_utils import StoppedByUser, run_wsl_or_raise, run_wsl_streaming, wsl_path
from . import helpers

_TIME_RE = re.compile(r"^Time\s*=\s*([\d.eE+-]+)\s*$")

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
        """
        self.target_time = target_time
        self.phase_start_time = time.time()
        self.current_time = None

    def solver_log_fn(self, line):
        """on_line callback for a solver's raw stdout - updates current_time
        from "Time = N" lines without appending anything to the log (an
        OpenFOAM run prints many lines per timestep; forwarding all of it
        would flood the log fast enough to scroll real narration out of
        view within seconds - see guvcfd.app._track_solver_time).
        """
        stripped = line.strip()
        if stripped.startswith("["):
            self.log_fn(stripped)
            return
        m = _TIME_RE.match(stripped)
        if m:
            self.current_time = m.group(1)

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
        pimple_delta_t=adv["pimple-delta-t"],
        log_fn=state.log_fn, should_stop=state.should_stop, solver_log_fn=state.solver_log_fn,
        should_pause=state.should_pause,
        sealed=settings["ach"] <= 0,
        **helpers.fan_kwargs(settings),
        **helpers.second_opening_kwargs(settings, "inlet2", room),
        **helpers.second_opening_kwargs(settings, "outlet2", room),
    )


def _run_decay(state, guv_path, case_dir, room, settings):
    adv = load_advanced_settings()
    _save_run_settings(case_dir, settings, guv_path)
    state.log_fn("=== Setting up mesh, flow field, and UV zones ===")
    summary = _setup_case_common(state, guv_path, case_dir, room, settings, adv)
    if state.should_stop():
        raise StoppedByUser("Stopped after case setup.")

    sealed = settings["ach"] <= 0
    case_dir_wsl = wsl_path(case_dir)
    control_dir = f"{case_dir}/no_UV"
    control_dir_wsl = wsl_path(control_dir)

    combined_end_time, control_end_time = helpers.decay_run_durations(
        settings["ach"], summary["eACH_uv_well_mixed_mean"], adv)
    write_interval = max(1, settings.get("pimple-write-interval", 10))
    set_control_dict_time(case_dir, end_time=combined_end_time,
                           write_interval=write_interval, delta_t=adv["pimple-delta-t"])

    if state.should_stop():
        raise StoppedByUser("Stopped before pimpleFoam.")
    if sealed:
        state.log_fn("Sealed room: skipping the UV-off control run - ventilation-only decay "
                      "rate is exactly 0 by construction (no opening for mass to leave through).")
    else:
        state.log_fn('=== Preparing UV-off control ("no_UV") ===')
        has_inlet2 = bool(settings.get("inlet2-enable"))
        prepare_ventilation_only_control(
            case_dir, control_dir, settings["ach"], room.x, room.y, room.z,
            settings["inlet-wall"], (settings["inlet-size-w"], settings["inlet-size-h"]),
            control_end_time, write_interval, pimple_delta_t=adv["pimple-delta-t"],
            inlet2_wall=settings["inlet2-wall"] if has_inlet2 else None,
            inlet2_size=(settings["inlet2-size-w"], settings["inlet2-size-h"]) if has_inlet2 else None,
            has_outlet2=bool(settings.get("outlet2-enable")),
            sealed=False, log_fn=state.log_fn, should_stop=state.should_stop,
        )

    state.log_fn(f"Running pimpleFoam: UV-on ({combined_end_time}s)"
                 + ("" if sealed else f" + UV-off control ({control_end_time}s)") + "...")
    state.begin_phase(combined_end_time)
    r_uv, r_control = _run_decay_pair(state, case_dir_wsl, None if sealed else control_dir_wsl)
    if state.should_stop():
        raise StoppedByUser("Stopped during pimpleFoam.")
    runs = [("UV-on", r_uv)] if sealed else [("UV-on", r_uv), ("UV-off control", r_control)]
    for label, r in runs:
        if r.returncode != 0 or "FOAM FATAL" in r.stdout or "Floating Point Exception" in r.stdout:
            tail = "\n".join(r.stdout.splitlines()[-25:]) or "(no output captured)"
            raise RuntimeError(f"{label} pimpleFoam failed (exit {r.returncode}):\n{tail}")

    state.log_fn("Running postProcess volAverage...")
    run_wsl_or_raise("postProcess -dict system/volAverageDict", case_dir_wsl, "postProcess volAverage")

    try:
        spatial_cov = spatial_coefficient_of_variation(read_latest_time_field(case_dir, "T"))
    except Exception:
        spatial_cov = None

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


def _run_decay_pair(state, case_dir_wsl, control_dir_wsl):
    """Run UV-on (and, unless control_dir_wsl is None, the UV-off control)
    pimpleFoam solves concurrently - see guvcfd.app._run_decay_pair for the
    identical design this mirrors."""
    results = {}
    errors = {}

    def run_one(name, cwd_wsl, on_line, log_prefix):
        try:
            callback = scenario_runs._throttled_solver_callback(
                state.log_fn, log_prefix, on_line=on_line,
                status_fn=state.live_status_fn, status_key=log_prefix)
            results[name] = run_wsl_streaming(
                "pimpleFoam 2>&1 | tee log.pimpleFoam", cwd_wsl,
                on_line=callback, should_stop=state.should_stop, kill_pattern="pimpleFoam",
                should_pause=state.should_pause,
            )
        except Exception as e:
            errors[name] = e
        finally:
            state.live_status_fn(log_prefix, None)

    threads = [threading.Thread(target=run_one, args=("uv", case_dir_wsl, state.solver_log_fn, "UV-on"))]
    if control_dir_wsl is not None:
        threads.append(threading.Thread(target=run_one, args=("control", control_dir_wsl, None, "control")))
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    if errors:
        raise next(iter(errors.values()))
    return results["uv"], results.get("control")


def _run_steady_state(state, guv_path, case_dir, room, settings):
    if settings["ach"] <= 0:
        raise ValueError("Sealed-room / ACH<=0 is only supported in Decay mode.")
    adv = load_advanced_settings()
    _save_run_settings(case_dir, settings, guv_path)

    state.log_fn("=== Setting up mesh, flow field, and UV zones ===")
    summary = _setup_case_common(state, guv_path, case_dir, room, settings, adv)
    if state.should_stop():
        raise StoppedByUser("Stopped after case setup.")

    fan_entry = None
    if settings.get("fan-enable"):
        direction = (0, 0, -1) if settings.get("fan-direction", "down") == "down" else (0, 0, 1)
        fan_entry = fan_fvoptions_entry(settings["fan-speed"], direction=direction)

    room_volume = room.x * room.y * room.z
    has_inlet2 = bool(settings.get("inlet2-enable"))
    has_outlet2 = bool(settings.get("outlet2-enable"))
    openings = [(settings["inlet-wall"], settings["inlet-size-w"] * settings["inlet-size-h"])]
    if has_inlet2:
        openings.append((settings["inlet2-wall"], settings["inlet2-size-w"] * settings["inlet2-size-h"]))
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
    state.begin_phase(phase1_iterations)
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
    with open(f"{case_dir}/results.json", "w") as f:
        json.dump(result, f, indent=2)
    state.results = result
    state.log_fn(f"Done. Reduction={result['reduction_pct']:.1f}%, "
                 f"eACH_uv={result['eACH_uv_steady_state']:.4g} /hr")
