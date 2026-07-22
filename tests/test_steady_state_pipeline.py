import inspect

import numpy as np

import guvcfd.steady_state_pipeline as ssp
from guvcfd.steady_state_pipeline import (
    _chunk_write_interval, _clear_phase1_checkpoint, _list_time_dirs, _point_phase_summary,
    _read_phase1_checkpoint, _rename_chunk_time_dirs, _room_phase_summary, _write_phase1_checkpoint,
    run_steady_state_scenario,
)


def test_chunk_write_interval_unaffected_by_full_size_chunks():
    # Normal case: chunk_size >= write_interval - no-op, at least one
    # write lands within the chunk already.
    assert _chunk_write_interval(100, 500) == 100
    assert _chunk_write_interval(100, 100) == 100


def test_chunk_write_interval_clamps_for_short_final_chunk():
    # Regression: a T-infinity early-stop chunk's remainder (e.g. 84
    # iterations left after several 500-iteration chunks) can be shorter
    # than the phase's normal write_interval (e.g. 100) - controlDict's
    # writeControl is "adjustableRunTime" (never touched by
    # set_control_dict_time, which only rewrites values), which does NOT
    # force a write at endTime the way "timeStep" mode would, so without
    # this clamp no time directory ever appears and _run_phase()'s "did a
    # new time directory show up" check incorrectly fails a run that
    # actually completed fine. Confirmed against a real failure: Scenario
    # Runs sweep, Z=3/ACH=1.5, phase2 endTime=84 with write_interval=100
    # produced "simpleFoam did not write any new time directory (found: '0')".
    assert _chunk_write_interval(100, 84) == 84


def _log(msg):
    pass


def test_run_steady_state_scenario_still_accepts_advanced_settings_params():
    # Regression guard: the Settings menu (app.py) calls this function with
    # explicit cell_size/nbins/source_size/plateau_rel_tol - if any of these
    # were ever renamed/removed, that call site would break silently (kwargs
    # just vanish into **nothing** until the next real WSL run). Locks in
    # both presence and the original defaults.
    params = inspect.signature(run_steady_state_scenario).parameters
    assert params["cell_size"].default == 0.1
    assert params["nbins"].default == 25
    assert params["source_size"].default == 0.3
    assert params["plateau_rel_tol"].default == 0.01
    assert params["window_frac"].default == 0.15
    assert "plateau_window" not in params  # replaced - plateau check now uses window_frac too
    assert params["t_inf_check_interval"].default is None
    assert params["t_inf_rel_tol"].default is None  # disabled by default - opt-in
    assert params["t_inf_streak"].default == 3
    assert params["keep_all_timesteps"].default is False  # opt-in - off keeps case dirs small


def test_rename_chunk_time_dirs_is_noop_at_zero_offset(monkeypatch):
    # The very first chunk of phase 1 has offset=0 - renaming "100" to
    # "100" would be a needless (and on some shells error-prone, "same
    # file") no-op, so this should skip the WSL round-trip entirely.
    calls = []
    monkeypatch.setattr(ssp, "run_wsl_or_raise", lambda cmd, *a, **k: calls.append(cmd))
    _rename_chunk_time_dirs("/some/case", 0, {"200", "400"})
    assert calls == []


def test_rename_chunk_time_dirs_is_noop_with_no_dir_names(monkeypatch):
    calls = []
    monkeypatch.setattr(ssp, "run_wsl_or_raise", lambda cmd, *a, **k: calls.append(cmd))
    _rename_chunk_time_dirs("/some/case", 1500, set())
    assert calls == []


def test_rename_chunk_time_dirs_shifts_only_the_given_names(monkeypatch):
    # Regression guard for the real corruption this fixes: renaming must
    # touch EXACTLY the given directories, never a blanket "every numbered
    # directory on disk" glob - otherwise, with keep_all_timesteps=True
    # (which never cleans old directories between chunks), an already-
    # renamed directory from an earlier chunk gets shifted again on every
    # subsequent chunk, compounding its offset (confirmed on a real run:
    # directory names inflated to 160,000+ despite the run only reaching
    # ~12,700 iterations).
    calls = []
    monkeypatch.setattr(ssp, "run_wsl_or_raise", lambda cmd, *a, **k: calls.append(cmd))
    _rename_chunk_time_dirs("/some/case", 1500, {"200", "400"})
    assert len(calls) == 1
    cmd = calls[0]
    assert "1500" in cmd
    assert "200" in cmd and "400" in cmd
    assert "[0-9]*" not in cmd  # no blanket glob - only the exact names given


def test_list_time_dirs_excludes_zero(monkeypatch):
    monkeypatch.setattr(ssp, "run_wsl_or_raise",
                         lambda cmd, *a, **k: type("R", (), {"stdout": "200\n400\n"})())
    assert _list_time_dirs("/some/case") == {"200", "400"}


def test_room_phase_summary_uses_windowed_mean_not_last_point():
    # A noisy-plateau live series - true mean ~0.31, but the raw last
    # sample can land off that by a fair bit (matches the real
    # live-volAverage validation: last-point reads swung several % from
    # the windowed average on a real turbulent run).
    t = np.arange(100, dtype=float)
    T = np.full(100, 0.31)
    T[-1] = 0.28  # a single noisy outlier at the very end
    live_room = (t, T)

    phase = _room_phase_summary(live_room, window_frac=0.15, converged=True,
                                 iterations="8000", sparse_t=t[::10], sparse_T=T[::10], log_fn=_log)

    assert phase["T_ss"] != T[-1]  # not just the last sample
    assert abs(phase["T_ss"] - 0.31) < 0.01  # close to the true plateau
    assert phase["T_ss_std"] > 0
    assert phase["T_ss_cv"] is not None
    assert phase["converged"] is True
    assert phase["iterations"] == "8000"
    assert phase["T_ss_window_frac"] == 0.15
    assert "live" in phase and "decay_curve" in phase


def test_room_phase_summary_window_span_matches_iteration_count():
    t = np.arange(0, 1000, 10, dtype=float)  # 100 points, spaced by 10
    T = np.full(100, 0.5)
    live_room = (t, T)
    phase = _room_phase_summary(live_room, window_frac=0.15, converged=True,
                                 iterations="1000", sparse_t=t, sparse_T=T, log_fn=_log)
    # window n = round(100 * 0.15) = 15 points -> span = t[-1] - t[-15]
    assert phase["T_ss_window_n"] == 15
    assert phase["T_ss_window_span"] == t[-1] - t[-15]


def test_point_phase_summary_matches_room_summary_windowing():
    t = np.arange(100, dtype=float)
    T = np.concatenate([np.zeros(80), np.full(20, 2.0)])  # jump partway through
    point = _point_phase_summary((t, T), window_frac=0.15)
    assert point["T_ss"] == 2.0  # trailing window is entirely post-jump
    assert point["T_ss_std"] < 1e-9  # ~0 (detrended fit leaves tiny float noise on exactly-flat data)
    assert point["t_seconds"] == t.tolist()
    assert point["volAverage_T"] == T.tolist()


# --- Phase 1 checkpoint: resuming without redoing the more expensive phase ---

def test_phase1_checkpoint_round_trips(tmp_path):
    assert _read_phase1_checkpoint(str(tmp_path)) is None  # nothing yet

    phase1_summary = {"T_ss": 1.047, "iterations": 12716, "converged": True}
    phase1_monitoring = {"exhaust": {"T_ss": 0.9}}
    _write_phase1_checkpoint(str(tmp_path), phase1_summary, phase1_monitoring,
                              G=0.027, Su=1.5, source_volume=0.018, n_source_cells=18)

    checkpoint = _read_phase1_checkpoint(str(tmp_path))
    assert checkpoint["phase1_summary"] == phase1_summary
    assert checkpoint["phase1_monitoring"] == phase1_monitoring
    assert checkpoint["G"] == 0.027
    assert checkpoint["Su"] == 1.5
    assert checkpoint["source_volume"] == 0.018
    assert checkpoint["n_source_cells"] == 18


def test_phase1_checkpoint_cleared_removes_it(tmp_path):
    _write_phase1_checkpoint(str(tmp_path), {"T_ss": 1.0, "iterations": 100}, {},
                              G=0.027, Su=1.5, source_volume=0.018, n_source_cells=18)
    assert _read_phase1_checkpoint(str(tmp_path)) is not None
    _clear_phase1_checkpoint(str(tmp_path))
    assert _read_phase1_checkpoint(str(tmp_path)) is None


def test_phase1_checkpoint_clear_is_a_noop_when_absent(tmp_path):
    # Must not raise just because there was nothing to clear (e.g. a
    # scenario that never needed to checkpoint - Phase 1 succeeded and
    # Phase 2 ran in the same call, no crash in between).
    _clear_phase1_checkpoint(str(tmp_path))


def test_phase1_checkpoint_corrupted_file_reads_as_none(tmp_path):
    (tmp_path / "phase1_checkpoint.json").write_text("{not valid json")
    assert _read_phase1_checkpoint(str(tmp_path)) is None
