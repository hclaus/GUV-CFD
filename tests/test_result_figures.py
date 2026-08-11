from guvcfd.result_figures import decay_figure, steady_state_figure


def _phase(T_ss, live=None, decay_curve=None, window_span=None):
    phase = {"T_ss": T_ss, "T_ss_window_span": window_span}
    if live is not None:
        phase["live"] = live
    if decay_curve is not None:
        phase["decay_curve"] = decay_curve
    return phase


def test_prefers_live_curve_when_decay_curve_is_missing_entirely():
    # Regression: p1.get("live", p1["decay_curve"]) always evaluates
    # p1["decay_curve"] to build the default argument, even when "live" IS
    # present - crashing with KeyError on any result that has "live" but
    # genuinely lacks "decay_curve" (confirmed live: importing a real
    # completed steady-state results.json). Must not raise.
    result = {
        "phase1": _phase(1.0, live={"t": [0, 1, 2], "T": [1.0, 0.9, 0.8]}),
        "phase2": _phase(0.3, live={"t": [0, 1, 2], "T": [0.8, 0.5, 0.3]}),
    }
    fig = steady_state_figure(result)  # must not raise
    # The "live" curve was actually used (its t/T values show up in the figure).
    assert list(fig.data[0].x) == [0, 1, 2]
    assert list(fig.data[0].y) == [100.0, 90.0, 80.0]


def test_falls_back_to_decay_curve_when_live_is_absent():
    # Older results.json predating live tracking - only decay_curve exists.
    result = {
        "phase1": _phase(1.0, decay_curve={"t": [0, 5, 10], "T": [1.0, 0.85, 0.7]}),
        "phase2": _phase(0.3, decay_curve={"t": [0, 5, 10], "T": [0.7, 0.5, 0.3]}),
    }
    fig = steady_state_figure(result)  # must not raise
    assert list(fig.data[0].x) == [0, 5, 10]


def test_prefers_live_over_decay_curve_when_both_present():
    result = {
        "phase1": _phase(1.0, live={"t": [0, 1], "T": [1.0, 0.9]},
                          decay_curve={"t": [0, 5], "T": [1.0, 0.8]}),
        "phase2": _phase(0.3, live={"t": [0, 1], "T": [0.9, 0.3]},
                          decay_curve={"t": [0, 5], "T": [0.9, 0.3]}),
    }
    fig = steady_state_figure(result)
    assert list(fig.data[0].x) == [0, 1]  # the dense "live" series, not the sparse one


def test_steady_state_figure_degrades_gracefully_for_a_trimmed_report_json():
    # Regression: scenario_runs._trim_report deliberately strips BOTH
    # "live" and "decay_curve" from a sweep's per-combo report.json
    # (summary numbers only, by design - see write_sweep_summary_csv).
    # Confirmed live: loading exactly this kind of file crashed the whole
    # Analysis tab with KeyError, hiding even the summary metrics that
    # WERE available in the file. Must not raise - falls back to a "no
    # curve data" placeholder instead.
    result = {"phase1": _phase(1.0), "phase2": _phase(0.3)}  # neither key present
    fig = steady_state_figure(result)  # must not raise
    assert len(fig.data) == 0  # placeholder, not a real curve
    assert "no curve data" in fig.layout.annotations[0].text.lower()


def test_decay_figure_degrades_gracefully_for_a_trimmed_report_json():
    # Decay-mode equivalent (scenario_runs._trim_decay_report strips the
    # top-level "decay_curve" entirely).
    result = {"ventilation_ach": 6.0, "eACH_uv_well_mixed": 40.0}  # no decay_curve at all
    fig = decay_figure(result)  # must not raise
    assert len(fig.data) == 0
    assert "no curve data" in fig.layout.annotations[0].text.lower()


def test_decay_figure_still_plots_when_decay_curve_is_present():
    result = {
        "decay_curve": {"t_seconds": [0, 10, 20], "volAverage_T": [1.0, 0.5, 0.25]},
        "ventilation_ach": 6.0, "eACH_uv_well_mixed": 40.0,
    }
    fig = decay_figure(result)
    assert list(fig.data[0].x) == [0, 10, 20]
    assert list(fig.data[0].y) == [1.0, 0.5, 0.25]
