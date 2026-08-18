import inspect

import numpy as np
import pytest

import guvcfd.steady_state_pipeline as ssp
from guvcfd.steady_state_pipeline import (
    _chunk_write_interval, _clear_phase1_checkpoint, _clear_phase2_pending, _list_time_dirs,
    _point_phase_summary, _read_phase1_checkpoint, _read_phase2_pending, _rename_chunk_time_dirs,
    _room_phase_summary, _run_phase, _write_phase1_checkpoint, _write_phase2_pending,
    compute_corrected_eACH_uv, compute_corrected_eACH_uv_from_control,
    compute_scaled_delta_t, resolve_phase_delta_ts,
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


def test_chunk_write_interval_snaps_down_when_chunk_size_not_a_multiple():
    # Regression: a FULL-size chunk that isn't a clean multiple of
    # write_interval wastes real solver progress too, not just short
    # remainder chunks. Confirmed on a live overnight run: the T-infinity
    # early-stop's hardcoded 500-iteration chunks against write_interval=200
    # (500 % 200 != 0) wrote snapshots at 200/400 but never at 500 -
    # "adjustableRunTime" never forces a write at the true chunk endTime -
    # so the solver's own last 100 iterations of real progress every chunk
    # were silently discarded (_run_phase's "latest" checkpoint fell back
    # to 400, not 500). 125 is the largest divisor of 500 that's <= 200.
    assert _chunk_write_interval(200, 500) == 125
    assert 500 % _chunk_write_interval(200, 500) == 0


def test_chunk_write_interval_always_evenly_divides_chunk_size():
    for write_interval in (1, 3, 7, 50, 100, 199, 200, 201, 500, 1000):
        for chunk_size in (1, 84, 100, 200, 333, 500, 501, 8000):
            result = _chunk_write_interval(write_interval, chunk_size)
            assert chunk_size % result == 0, (write_interval, chunk_size, result)
            assert result <= min(write_interval, chunk_size)


def test_compute_scaled_delta_t_stays_at_1_when_budget_already_covers_target():
    # A high-ACH case (short residence time) already comfortably covers
    # ~5.3 residence times within its iteration budget at delta_t=1 - must
    # not be slowed down (regression: never scale below the historical 1).
    assert compute_scaled_delta_t(effective_rate_per_hr=20.0, n_iterations=8000) == 1


def test_compute_scaled_delta_t_scales_up_for_low_rate_short_budget():
    # ACH=3 case this was validated against: theta=3600/(0.7*3)=1714.3s,
    # target_cycles=ln(200)=5.298, ideal_dt=5.298*1714.3/1500=6.06 -> 6.
    dt = compute_scaled_delta_t(effective_rate_per_hr=0.7 * 3.0, n_iterations=1500)
    assert dt == 6


def test_compute_scaled_delta_t_floors_at_1_never_below():
    # Even a rate so high the ideal deltaT would be a fraction < 1 must
    # floor at 1, not slow the solver down below its historical default.
    assert compute_scaled_delta_t(effective_rate_per_hr=1000.0, n_iterations=100) == 1


def test_compute_scaled_delta_t_nonpositive_rate_returns_1():
    # Same fallback _settling_iterations uses for lambda_per_hr <= 0 - not
    # physically meaningful (would need infinite time), so delta_t can't
    # help; only a bigger n_iterations budget could.
    assert compute_scaled_delta_t(effective_rate_per_hr=0.0, n_iterations=1500) == 1
    assert compute_scaled_delta_t(effective_rate_per_hr=-1.0, n_iterations=1500) == 1


def test_compute_scaled_delta_t_returns_int_type():
    assert isinstance(compute_scaled_delta_t(effective_rate_per_hr=2.0, n_iterations=1500), int)


_BASE_ADV = {
    "deltat-scaling-enabled": True, "deltat-effective-fraction": 0.7,
    "deltat-target-fraction": 0.995, "keep-all-timesteps": False,
}


def test_resolve_phase_delta_ts_disabled_returns_ones():
    adv = dict(_BASE_ADV, **{"deltat-scaling-enabled": False})
    assert resolve_phase_delta_ts(3.0, 15.0, 1500, 1000, adv) == (1, 1)


def test_resolve_phase_delta_ts_incompatible_with_keep_all_timesteps():
    # _run_phase raises if both delta_t != 1 and keep_all_timesteps are
    # requested together (see its own guard) - resolve this combination to
    # (1, 1) upstream instead of ever producing a value that would crash.
    adv = dict(_BASE_ADV, **{"keep-all-timesteps": True})
    assert resolve_phase_delta_ts(3.0, 15.0, 1500, 1000, adv) == (1, 1)


def test_resolve_phase_delta_ts_applies_effective_fraction_to_both_phases():
    # Phase 1 uses ach alone; Phase 2 uses ach + eACH_uv_well_mixed - both
    # derated by deltat-effective-fraction before computing residence time.
    dt1, dt2 = resolve_phase_delta_ts(3.0, 15.0, 1500, 1000, _BASE_ADV)
    assert dt1 == compute_scaled_delta_t(0.7 * 3.0, 1500, target_fraction=0.995)
    assert dt2 == compute_scaled_delta_t(0.7 * (3.0 + 15.0), 1000, target_fraction=0.995)


def test_phase1_extrapolation_undecided_copies_latest_to_zero_before_raising(tmp_path, monkeypatch):
    # Regression: _write_phase1_pending's docstring promises "0/ already
    # holds Phase 1's current (mid-run, undecided) state" for a later
    # resume to build on - but _copy_latest_to_zero used to run AFTER the
    # Phase1ExtrapolationUndecided raise, so it never executed when the
    # exception actually fired. A resume would then silently continue from
    # a stale 0/ (however many non-final chunks behind) while reporting
    # iteration counts as if it hadn't - confirmed as a real gap on a live
    # run. Verifies the fix: _copy_latest_to_zero is called (with the
    # correct final directory name) even though the call still raises.
    (tmp_path / "system").mkdir()
    (tmp_path / "constant").mkdir()
    for fn in ("ensure_simple_fvsolution", "disable_simple_residual_control", "write_vol_average_dict",
               "write_source_topo_set_dict", "write_fvoptions_file", "restore_boundary_conditions"):
        monkeypatch.setattr(ssp, fn, lambda *a, **k: None)
    monkeypatch.setattr(ssp, "splice_fv_options_into_control_dict", lambda *a, **k: (None, 1, 1))

    class _FakeResult:
        stdout = "cellSet sourceZoneCells now size 64"
        returncode = 0
    monkeypatch.setattr(ssp, "run_wsl_or_raise", lambda *a, **k: _FakeResult())

    copy_calls = []
    monkeypatch.setattr(ssp, "_copy_latest_to_zero", lambda case_dir_wsl, latest, include_T, log_fn: (
        copy_calls.append(latest)))

    def fake_run_phase(*a, **k):
        # Shape matches _run_phase's real return tuple - stopped_via_tinf1=False
        # is what triggers the raise below (ceiling reached, never stabilized).
        return ("1500", 1500, np.array([0, 1]), np.array([0.1, 0.2]), False,
                {"room": (np.array([0, 1]), np.array([0.1, 0.2]))}, False, [None, None], {"flow": {}, "weighted_t": {}})
    monkeypatch.setattr(ssp, "_run_phase", fake_run_phase)

    with pytest.raises(ssp.Phase1ExtrapolationUndecided):
        run_steady_state_scenario(
            str(tmp_path), 4.0, 5.0, 2.7, ach=6.0, Z=6.0,
            phase1_iterations=1500, phase2_iterations=1500,
            t_inf_check_interval=400, t_inf_rel_tol=0.02,
            # Explicit - this test is specifically about the gate's raise-
            # time behavior, so it must not depend on the function's own
            # default (now False - see phase1_extrapolation_gate's
            # docstring) to make the raise happen.
            phase1_extrapolation_gate=True,
            log_fn=lambda m: None,
        )

    assert copy_calls == ["1500"]  # the copy ran, using the true final directory name


def test_phase1_extrapolation_gate_defaults_off_does_not_raise(tmp_path, monkeypatch):
    # The default flipped to False (see phase1_extrapolation_gate's own
    # docstring) - a case whose extrapolation never stabilized (same
    # stopped_via_tinf1=False fixture as the raise test above) must now
    # complete normally by default, not pause. Confirmed as a real, live
    # blocker on a 2-combination sweep before this fix: sweep mode has no
    # resume UX for this pause at all, so a stuck combo just failed outright.
    class _FakeResult:
        stdout = "cellSet sourceZoneCells now size 64"
        returncode = 0
    for fn in ("ensure_simple_fvsolution", "disable_simple_residual_control", "write_vol_average_dict",
               "write_source_topo_set_dict", "write_fvoptions_file", "restore_boundary_conditions"):
        monkeypatch.setattr(ssp, fn, lambda *a, **k: None)
    monkeypatch.setattr(ssp, "splice_fv_options_into_control_dict", lambda *a, **k: (None, 1, 1))
    monkeypatch.setattr(ssp, "run_wsl_or_raise", lambda *a, **k: _FakeResult())
    monkeypatch.setattr(ssp, "_copy_latest_to_zero", lambda *a, **k: None)

    def fake_run_phase(*a, **k):
        return ("1500", 1500, np.array([0, 1]), np.array([0.1, 0.2]), False,
                {"room": (np.array([0, 1]), np.array([0.1, 0.2]))}, False, [None, None], {"flow": {}, "weighted_t": {}})
    monkeypatch.setattr(ssp, "_run_phase", fake_run_phase)

    summary = run_steady_state_scenario(
        str(tmp_path), 4.0, 5.0, 2.7, ach=6.0, Z=6.0,
        phase1_iterations=1500, phase2_iterations=1500,
        t_inf_check_interval=400, t_inf_rel_tol=0.02,
        phase1_only=True,
        log_fn=lambda m: None,
    )
    assert summary["phase1"]["converged"] is False  # plateau CV verdict still visible, just not blocking


def test_run_phase_rejects_delta_t_with_keep_all_timesteps():
    # This guard must fire before any filesystem/WSL work - called with
    # deliberately-invalid dummy paths to confirm it does.
    with pytest.raises(ValueError, match="keep_all_timesteps"):
        _run_phase(
            "nonexistent_case_dir", "nonexistent_case_dir_wsl", n_iterations=100, write_interval=50,
            window_frac=0.15, plateau_rel_tol=0.01, log_fn=_log,
            keep_all_timesteps=True, delta_t=2,
        )


def test_phase_solver_callback_falls_back_unchanged_without_status_fn():
    # No status_fn (single-run mode - progress there comes from
    # solver_log_fn/app._track_solver_time instead, which has never shown
    # per-iteration lines in its own log) - must behave exactly like the
    # old `solver_log_fn or log_fn` it replaced.
    solver_lines = []
    callback = ssp._phase_solver_callback(_log, solver_lines.append, None, "key")
    callback("Time = 1")
    assert solver_lines == ["Time = 1"]
    assert ssp._phase_solver_callback(_log, None, None, "key") is _log


def test_phase_solver_callback_redirects_time_lines_to_status_fn():
    # A concurrent sweep combination (status_fn given): "Time = N" banners
    # go to status_fn instead of the scrolling log, so several
    # combinations solving at once don't flood it - solver_log_fn still
    # gets every raw line either way (preserves single-run-style progress
    # tracking even inside a sweep).
    solver_lines = []
    status_calls = []
    callback = ssp._phase_solver_callback(
        _log, solver_lines.append, lambda k, m: status_calls.append((k, m)), "Z=6/ACH=3/Phase1")

    callback("Time = 42.5")
    callback("smoothSolver:  Solving for Ux, Initial residual = 0.01")

    assert status_calls == [("Z=6/ACH=3/Phase1", "Time = 42.5")]
    assert solver_lines == ["Time = 42.5", "smoothSolver:  Solving for Ux, Initial residual = 0.01"]


def test_phase_solver_callback_always_shows_time_even_when_delta_t_scaled():
    # Regression (2026-08-08): a previous version relabeled this "Iteration
    # N" (dividing Time back out by delta_t) specifically when delta_t != 1,
    # so the same status field showed "Time" for some combinations and
    # "Iteration" for others with nothing in the UI explaining why -
    # confirmed as a real point of confusion. Now always "Time", in
    # seconds, for every combination alike, regardless of delta_t.
    solver_lines = []
    status_calls = []
    callback = ssp._phase_solver_callback(
        _log, solver_lines.append, lambda k, m: status_calls.append((k, m)), "Z=6/ACH=6/Phase1", delta_t=3)

    callback("Time = 840")

    assert status_calls == [("Z=6/ACH=6/Phase1", "Time = 840")]
    assert solver_lines == ["Time = 840"]  # raw line unconverted


def test_phase_solver_callback_leaves_time_unconverted_at_delta_t_one():
    status_calls = []
    callback = ssp._phase_solver_callback(
        _log, None, lambda k, m: status_calls.append((k, m)), "key", delta_t=1)
    callback("Time = 842")
    assert status_calls == [("key", "Time = 842")]


def test_phase_solver_callback_iteration_base_makes_progress_cumulative():
    # Regression: every chunk's own OpenFOAM "Time" restarts from 0 (see
    # _run_phase's "every chunk starts fresh at time-label 0") -
    # iteration_base (a cumulative ITERATION count from prior chunks) is
    # converted to a TIME offset (iteration_base * delta_t) and added to
    # this chunk's own raw Time, so the displayed number is always the
    # true cumulative elapsed time, not chunk-local progress that resets
    # every ~400 iterations. At delta_t=3, iteration_base=800 (2400s of
    # prior chunks) plus this chunk's own "Time = 1161" gives 3561.
    status_calls = []
    callback = ssp._phase_solver_callback(
        _log, None, lambda k, m: status_calls.append((k, m)), "key", delta_t=3, iteration_base=800)
    callback("Time = 1161")
    assert status_calls == [("key", "Time = 3561")]


def test_phase_solver_callback_iteration_base_applies_even_at_delta_t_one():
    # A nonzero iteration_base must still make progress cumulative even
    # when delta_t itself is 1 (Time already equals chunk-local iterations
    # 1:1, but still needs the chunk's own starting offset added).
    status_calls = []
    callback = ssp._phase_solver_callback(
        _log, None, lambda k, m: status_calls.append((k, m)), "key", delta_t=1, iteration_base=400)
    callback("Time = 120")
    assert status_calls == [("key", "Time = 520")]


def test_phase_solver_callback_appends_total_time_suffix_when_given():
    status_calls = []
    callback = ssp._phase_solver_callback(
        _log, None, lambda k, m: status_calls.append((k, m)), "key", delta_t=3, iteration_base=800,
        total_time=4500)
    callback("Time = 1161")
    assert status_calls == [("key", "Time = 3561 of 4500 total seconds")]


def test_phase_solver_callback_omits_total_time_suffix_when_not_given():
    status_calls = []
    callback = ssp._phase_solver_callback(
        _log, None, lambda k, m: status_calls.append((k, m)), "key")
    callback("Time = 100")
    assert status_calls == [("key", "Time = 100")]


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
    assert params["phase1_only"].default is False  # off by default - normal single/per-Z runs do both phases
    assert params["measured_ventilation_ach"].default is None  # off by default - no control run given
    assert params["phase1_delta_t"].default == 1  # historical deltaT - callers opt in via resolve_phase_delta_ts
    assert params["phase2_delta_t"].default == 1


def test_compute_corrected_eACH_uv_from_control_matches_manual_mass_balance():
    # lambda_total_actual = G/(V*T_ss2); eACH_uv = lambda_total_actual minus
    # the (already measured, e.g. by a UV-off control run) ventilation rate -
    # same "total minus measured ventilation" subtraction
    # decay_analysis.compute_effective_eACH does, just derived from a
    # steady-state ratio instead of a transient curve fit.
    Su, source_volume, room_volume = 2.4672, 0.012, 39.4752
    T_ss2 = 0.11213866470568581
    ventilation_ach_measured = 2.455280997637921  # e.g. from a control run

    G = Su * source_volume
    lambda_total_actual = G / (room_volume * T_ss2) * 3600
    expected = lambda_total_actual - ventilation_ach_measured

    result = compute_corrected_eACH_uv_from_control(
        T_ss2, Su, source_volume, room_volume, ventilation_ach_measured)
    assert result == pytest.approx(expected)


def test_compute_corrected_eACH_uv_from_control_handles_zero_T_ss2():
    assert compute_corrected_eACH_uv_from_control(0.0, 2.4672, 0.012, 39.4752, 2.46) is None


def test_compute_corrected_eACH_uv_still_works_unchanged():
    # The old (Phase-1-T_ss1-derived) path is kept for the single-run
    # fallback - unchanged behavior, still returns (ventilation_ach_measured,
    # eACH_uv_corrected).
    ach_measured, eACH = compute_corrected_eACH_uv(
        T_ss1=0.6010924162620547, T_ss2=0.11213866470568581,
        Su=2.4672, source_volume=0.012, room_volume=39.4752)
    assert ach_measured == pytest.approx(4.491821768090478)
    assert eACH == pytest.approx(19.585511478977345)


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


def test_point_phase_summary_extrapolates_t_infinity_like_room_summary():
    # Regression: monitoring points used to get only the windowed mean,
    # never the T-infinity extrapolation the room average already uses -
    # a real gap, since a point (a small, specific zone, often away from
    # the main flow) can plausibly take LONGER to settle than the room
    # average does, making it if anything MORE exposed to the windowed-
    # average bias the extrapolation exists to correct. Same synthetic
    # exponential-approach curve test_decay_analysis.py's own
    # fit_asymptotic_value test uses, run still short of full convergence
    # (last sample still below the true asymptote) so the windowed mean
    # and the extrapolation are expected to genuinely differ.
    true_Tinf, true_A, true_tau = 2.0, 0.5, 200.0
    t = np.arange(0, 900, 1.0)
    T = true_Tinf - true_A * np.exp(-t / true_tau)

    point = _point_phase_summary((t, T), window_frac=0.15)

    assert point["T_inf_extrapolated"] is not None
    assert abs(point["T_inf_extrapolated"] - true_Tinf) < 0.01
    assert point["T_inf_extrapolation_detail"] is not None
    assert point["T_ss"] < point["T_inf_extrapolated"]  # windowed mean still below the true asymptote


def test_point_phase_summary_extrapolation_none_when_fit_unavailable():
    # A step function (test_point_phase_summary_matches_room_summary_
    # windowing's own fixture) has no exponential-approach shape to fit -
    # T_inf_extrapolated must be None, not crash, exactly like
    # _room_phase_summary's own "None when the fit doesn't converge" rule.
    t = np.arange(100, dtype=float)
    T = np.concatenate([np.zeros(80), np.full(20, 2.0)])
    point = _point_phase_summary((t, T), window_frac=0.15)
    assert point["T_inf_extrapolated"] is None
    assert point["T_inf_extrapolation_detail"] is None


def _oscillating_room_series(n=3000, period=400, amplitude=0.15, mean=0.6):
    # A persistent, non-decaying oscillation (no damping term at all) -
    # matches the real incident this test guards: a genuinely unconverged
    # curve whose exponential-approach fit can still look well-constrained
    # over a short trailing window, and whose reported T_ss is a windowed
    # mean of data that never actually plateaued.
    t = np.arange(n, dtype=float)
    T = mean + amplitude * np.sin(2 * np.pi * t / period)
    return t, T


def test_room_phase_summary_rejects_poorly_constrained_extrapolation():
    # Regression test for a real, confirmed incident: _room_phase_summary's
    # own fit_asymptotic_value call was never gated by fit_cv, unlike
    # _run_phase's live accept/reject decision - a real Phase 2 fit with
    # fit_cv=7.45% (3.7x a 2% tolerance) was reported as if trustworthy,
    # with nothing in results.json indicating it wouldn't have passed the
    # same bar the live decision uses.
    t, T = _oscillating_room_series()
    phase = _room_phase_summary((t, T), window_frac=0.15, converged=False,
                                 iterations=len(t), sparse_t=t[::10], sparse_T=T[::10],
                                 log_fn=_log, t_inf_rel_tol=0.02)
    # Whatever fit_asymptotic_value produced on this oscillating data, a
    # fit_cv this bad must be rejected, not silently reported.
    assert phase["T_inf_extrapolated"] is None
    assert phase["T_inf_extrapolation_detail"] is None


def test_room_phase_summary_no_gate_when_t_inf_rel_tol_not_given():
    # Callers that never resolved a real t_inf_rel_tol (t_inf_rel_tol=None,
    # the default) must see the old, ungated behavior - this parameter is
    # additive, not a behavior change for existing callers that don't pass it.
    true_Tinf, true_A, true_tau = 2.0, 0.5, 200.0
    t = np.arange(0, 900, 1.0)
    T = true_Tinf - true_A * np.exp(-t / true_tau)
    phase = _room_phase_summary((t, T), window_frac=0.15, converged=True,
                                 iterations=len(t), sparse_t=t[::10], sparse_T=T[::10], log_fn=_log)
    assert phase["T_inf_extrapolated"] is not None


def test_room_phase_summary_warns_loudly_when_not_converged():
    # Regression test for a real, confirmed incident: `converged` was
    # already computed and returned, but the only place it was ever
    # surfaced was a passive text label buried in the docx report - a run
    # whose curve never plateaued produced a clean, confident-looking
    # reduction_pct with no visible caveat anywhere in the live log.
    t, T = _oscillating_room_series()
    logged = []
    _room_phase_summary((t, T), window_frac=0.15, converged=False,
                         iterations=len(t), sparse_t=t[::10], sparse_T=T[::10],
                         log_fn=logged.append, t_inf_rel_tol=0.02)
    assert any(line.startswith("  WARNING:") and "did NOT plateau" in line for line in logged)


def test_room_phase_summary_no_warning_when_converged():
    t = np.arange(100, dtype=float)
    T = np.full(100, 0.31)
    logged = []
    _room_phase_summary((t, T), window_frac=0.15, converged=True,
                         iterations=100, sparse_t=t[::10], sparse_T=T[::10], log_fn=logged.append)
    assert not any("WARNING" in line for line in logged)


def test_point_phase_summary_rejects_poorly_constrained_extrapolation():
    # Same gate as the room-level test above, for a monitoring point.
    t, T = _oscillating_room_series()
    point = _point_phase_summary((t, T), window_frac=0.15, t_inf_rel_tol=0.02)
    assert point["T_inf_extrapolated"] is None
    assert point["T_inf_extrapolation_detail"] is None


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


# --- Phase 2 pending: resuming a stop at any chunk boundary (2026-08-18) ---
# Unlike Phase 1, Phase 2 has no single pause-and-ask decision point -
# T-infinity is an early-stop nicety only there, so a plain user Stop can
# land at any chunk boundary, and every one of them needs to be resumable.

def test_phase2_pending_round_trips(tmp_path):
    assert _read_phase2_pending(str(tmp_path)) is None  # nothing yet

    accumulated = {"room": ([0.0, 1.0, 2.0], [0.1, 0.15, 0.18])}
    _write_phase2_pending(str(tmp_path), total_run=2, accumulated=accumulated, tinf_history=[None, 0.5])

    pending = _read_phase2_pending(str(tmp_path))
    assert pending["total_run"] == 2
    assert pending["accumulated"] == {"room": [[0.0, 1.0, 2.0], [0.1, 0.15, 0.18]]}
    assert pending["tinf_history"] == [None, 0.5]


def test_phase2_pending_cleared_removes_it(tmp_path):
    _write_phase2_pending(str(tmp_path), total_run=5, accumulated={"room": ([], [])}, tinf_history=[])
    assert _read_phase2_pending(str(tmp_path)) is not None
    _clear_phase2_pending(str(tmp_path))
    assert _read_phase2_pending(str(tmp_path)) is None


def _fake_wsl_ok(*a, **k):
    from types import SimpleNamespace
    return SimpleNamespace(stdout="", stderr="", returncode=0)


def test_run_phase_resumes_seeded_state_and_persists_then_clears_pending(tmp_path, monkeypatch):
    # End-to-end (real chunk loop, WSL/solver calls mocked) regression test
    # for the actual resume mechanism added today: seed a chunk that
    # ALREADY has 10 prior iterations' worth of accumulated history (as if
    # read back from a previous, stopped attempt's own phase2_pending.json),
    # run exactly one more 5-iteration chunk to completion, and confirm
    # both the cumulative iteration count AND the full prior + new
    # accumulated series survive - not just the post-resume tail.
    case_dir = str(tmp_path)
    (tmp_path / "system").mkdir()
    # Already includes volAverageLive1 so the live-tracking splice step -
    # irrelevant to what this test is checking - is a no-op.
    (tmp_path / "system" / "controlDict").write_text("functions {\n  volAverageLive1 {}\n}\n")

    monkeypatch.setattr(ssp, "set_control_dict_time", lambda *a, **k: None)
    monkeypatch.setattr(ssp, "set_function_write_interval", lambda *a, **k: None)
    monkeypatch.setattr(ssp, "run_wsl_or_raise", _fake_wsl_ok)

    from types import SimpleNamespace
    monkeypatch.setattr(ssp, "run_wsl_streaming", lambda *a, **k: SimpleNamespace(stdout="", returncode=0))

    # dirs_before empty, dirs_after {"5"} - a fresh chunk-relative directory
    # (every chunk restarts OpenFOAM's own Time at 0, see _run_phase's own
    # docstring), regardless of how many iterations already ran before it.
    list_calls = {"n": 0}

    def fake_list_time_dirs(case_dir_wsl):
        list_calls["n"] += 1
        return set() if list_calls["n"] % 2 else {"5"}
    monkeypatch.setattr(ssp, "_list_time_dirs", fake_list_time_dirs)

    monkeypatch.setattr(ssp, "read_vol_average_dat", lambda path: (
        np.array([1.0, 2.0, 3.0, 4.0, 5.0]), np.array([0.2, 0.22, 0.24, 0.26, 0.28])))

    prior_t = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
    prior_T = [0.0, 0.02, 0.04, 0.06, 0.08, 0.1, 0.12, 0.14, 0.16, 0.18]
    # _run_phase mutates its resume_accumulated argument's own lists in
    # place (extends them chunk by chunk) - pass copies, not the
    # originals, so this test's own expected values below can't be
    # silently mutated out from under it by the very call being tested.
    prior_accumulated = {"room": (list(prior_t), list(prior_T))}

    result = _run_phase(
        case_dir, case_dir, n_iterations=15, write_interval=5, window_frac=0.15, plateau_rel_tol=0.01,
        log_fn=_log, resume_total_run=10, resume_accumulated=prior_accumulated,
        resume_tinf_history=["stale entry - t_inf_rel_tol is None here, never consulted"],
        pending_case_dir=case_dir,
    )
    final_dir_name, total_run, sparse_t, sparse_T, converged, live_curves, stopped_via_tinf, \
        tinf_history, mass_balance_flux = result

    assert total_run == 15  # 10 resumed + 5 from this one chunk
    live_t, live_T = live_curves["room"]
    assert len(live_t) == 15  # the resumed prior 10 points PLUS this chunk's own 5 - not just the 5
    assert list(live_t[:10]) == prior_t  # prior history preserved verbatim
    assert list(live_t[10:]) == [11.0, 12.0, 13.0, 14.0, 15.0]  # new chunk's times, offset by resume_total_run

    # Finished normally (total_run >= n_iterations) - the pending marker
    # this same call wrote mid-chunk must be cleared, not left behind.
    assert _read_phase2_pending(case_dir) is None


def test_phase1_checkpoint_clear_is_a_noop_when_absent(tmp_path):
    # Must not raise just because there was nothing to clear (e.g. a
    # scenario that never needed to checkpoint - Phase 1 succeeded and
    # Phase 2 ran in the same call, no crash in between).
    _clear_phase1_checkpoint(str(tmp_path))


def test_phase1_checkpoint_corrupted_file_reads_as_none(tmp_path):
    (tmp_path / "phase1_checkpoint.json").write_text("{not valid json")
    assert _read_phase1_checkpoint(str(tmp_path)) is None
