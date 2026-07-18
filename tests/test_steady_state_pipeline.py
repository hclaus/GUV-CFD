import inspect

import numpy as np

from guvcfd.steady_state_pipeline import _point_phase_summary, _room_phase_summary, run_steady_state_scenario


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
