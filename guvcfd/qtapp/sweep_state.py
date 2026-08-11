"""Z x ACH sweep orchestration + state - the native equivalent of
guvcfd.app's _scenario_state/_scenario_status_update/_launch_scenario_sweep.
Calls scenario_runs.run_decay_sweep()/run_sweep() directly - both already
fully GUI-independent, taking the same log_fn/should_stop/should_pause/
status_fn/on_combo_done callback shapes run_pipeline.setup_case() etc. do,
so this is a thin orchestration wrapper, not a reimplementation.

Simplification vs. the web app: a sweep has no resume UX for a combination
that hits FlowConvergenceUndecided/Phase1ExtrapolationUndecided - same as
the web app's own sweep path (scenario_runs.run_sweep's own docstring:
"no resume UX for sweeps... a stuck combo just fails"). That combo's
on_combo_done fires with status="error" and the exception message as
detail; the rest of the sweep continues.
"""
import threading
import time

from .. import scenario_runs


class SweepState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.status = "idle"  # idle / running / done / error / stopped
        self.log = []
        self.combos = []  # [(z, ach), ...]
        self.results = {}  # (z, ach) -> {"status": "done"/"error", "detail": trimmed result or message}
        self.live_status = {}
        self.start_time = None
        self.error = None
        self.stop_requested = False
        self.pause_requested = False

    def log_fn(self, msg):
        self.log.append(str(msg))
        if len(self.log) > 5000:
            del self.log[: len(self.log) - 5000]

    def should_stop(self):
        return self.stop_requested

    def should_pause(self):
        return self.pause_requested

    def status_fn(self, key, msg):
        """Per-stream "latest Time = N", overwritten in place - several
        concurrent combinations each printing their own line every step
        would otherwise flood the log the same way a single decay run's
        UV-on/control pair would (see run_state.RunState.live_status_fn)."""
        if msg is None:
            self.live_status.pop(key, None)
        else:
            self.live_status[key] = msg

    def on_combo_done(self, z, ach, status, detail):
        self.results[(z, ach)] = {"status": status, "detail": detail}

    def live_status_text(self):
        return "\n".join(f"[{k}] {self.live_status[k]}" for k in sorted(self.live_status))


def launch_extend(state, case_dirs, end_time, write_interval):
    """Sequentially extend each decay case_dir to end_time on a background
    thread - the native equivalent of guvcfd.app's _extend_pipeline_thread
    (see the "Extend / modify simulations" dialog's own "Extend specific
    run(s)" action). Reuses SweepState wholesale rather than a dedicated
    class - its log/combos/results/status fields already have exactly the
    shape this needs (one entry per (z, ach), a status/detail per entry),
    so the Run tab's existing sweep-table rendering works unchanged for
    this too. Not folded into run_sweep_concurrent/scenario_runs.run_sweep,
    since this is a small, explicit, user-picked list, not a Z x ACH
    cross-product - see the dialog's own docstring.
    """
    state.reset()
    state.status = "running"
    state.combos = [(z, ach) for _, z, ach in case_dirs]
    state.start_time = time.time()

    def worker():
        for case_dir, z, ach in case_dirs:
            if state.stop_requested:
                break
            state.log_fn(f"[Z={z}/ACH={ach}] Extending to {end_time}s...")
            try:
                scenario_runs.continue_decay(
                    case_dir, end_time, write_interval, log_fn=state.log_fn, should_stop=state.should_stop,
                    should_pause=state.should_pause,
                )
                state.results[(z, ach)] = {"status": "done", "detail": None}
                state.log_fn(f"[Z={z}/ACH={ach}] Done.")
            except scenario_runs.StoppedByUser:
                state.log_fn(f"[Z={z}/ACH={ach}] Stopped.")
                break
            except Exception as e:
                state.log_fn(f"[Z={z}/ACH={ach}] ERROR: {e}")
                state.results[(z, ach)] = {"status": "error", "detail": str(e)}
        state.status = "stopped" if state.stop_requested else "done"

    threading.Thread(target=worker, daemon=True).start()


def launch_sweep(state, guv_path, settings_path, project_dir, room, settings, adv, z_values, ach_values):
    """Start a sweep on a background daemon thread. state must be idle -
    caller (the Run tab) is responsible for checking that."""
    state.reset()
    state.status = "running"
    state.combos = scenario_runs.sweep_combinations(z_values, ach_values)
    state.start_time = time.time()

    def worker():
        try:
            sweep_fn = (scenario_runs.run_decay_sweep if settings["sim-type"] == "decay"
                        else scenario_runs.run_sweep)
            sweep_fn(
                guv_path, settings_path, project_dir, room, settings, adv, z_values, ach_values,
                log_fn=state.log_fn, should_stop=state.should_stop, on_combo_done=state.on_combo_done,
                status_fn=state.status_fn, should_pause=state.should_pause,
                # Without this, run_pipeline._run_phase()'s on_line=solver_log_fn
                # falls back to log_fn - every raw per-iteration solver line
                # (residuals, for every iteration, from every concurrently-
                # building ACH group) would flood the same log the coarse
                # narration uses, instead of just the throttled "[Z=.../
                # ACH=...] Time = N" lines _prefixed_log_fn already produces -
                # confirmed live: with 3 ACH groups converging at once, this
                # made the log view look completely frozen (it wasn't - the
                # solvers were fine, the log was just drowning). Matches
                # guvcfd.app._launch_scenario_sweep's identical fix exactly.
                solver_log_fn=lambda line: state.log_fn(line) if line.strip().startswith("[") else None,
            )
            state.status = "stopped" if state.stop_requested else "done"
        except Exception as e:
            state.error = str(e)
            state.log_fn(f"ERROR: {e}")
            state.status = "error"

    threading.Thread(target=worker, daemon=True).start()
