import json
import math

import numpy as np

from guvcfd.decay_analysis import (
    compute_effective_eACH, windowed_stats, write_results_summary, check_plateau_windowed,
    windowed_stats_detrended, fit_asymptotic_value, check_t_infinity_stability,
    mechanical_mixing_efficiency_pct, spatial_coefficient_of_variation,
)


def _synthetic_decay(lambda_per_s, t_max=1000, dt=10, T0=1.0):
    t = np.arange(0, t_max + dt, dt, dtype=float)
    T = T0 * np.exp(-lambda_per_s * t)
    return t, T


def test_nominal_baseline_matches_uncorrected_behavior():
    lambda_total = 0.005  # /s, e.g. ventilation(3/hr) + UV(~15/hr) combined
    t, T = _synthetic_decay(lambda_total)
    fit = compute_effective_eACH(t, T, ventilation_ach=3.0)
    assert math.isclose(fit["lambda_total_effective_per_s"], lambda_total, rel_tol=1e-6)
    expected_eACH = (lambda_total - 3.0 / 3600.0) * 3600.0
    assert math.isclose(fit["eACH_uv_effective"], expected_eACH, rel_tol=1e-6)


def test_measured_ventilation_baseline_overrides_nominal():
    lambda_total = 0.005
    t, T = _synthetic_decay(lambda_total)
    # Measured ventilation-only rate corresponds to 2.67/hr, not the nominal 3.0/hr.
    measured_lambda = 2.67 / 3600.0
    fit = compute_effective_eACH(
        t, T, ventilation_ach=3.0, ventilation_lambda_per_s=measured_lambda)
    expected_eACH = (lambda_total - measured_lambda) * 3600.0
    assert math.isclose(fit["eACH_uv_effective"], expected_eACH, rel_tol=1e-6)
    # Using the smaller measured baseline attributes MORE of the decay to UV.
    fit_nominal = compute_effective_eACH(t, T, ventilation_ach=3.0)
    assert fit["eACH_uv_effective"] > fit_nominal["eACH_uv_effective"]


def test_fit_effective_decay_rate_ci_widens_with_noise():
    # A perfect synthetic exponential has ~zero residual, so its CI is
    # essentially a point; adding noise should give a real, non-trivial CI
    # around the point estimate (always true by construction - not a
    # containment check against the true rate, which a single random draw
    # can legitimately miss ~5% of the time by design of a 95% CI).
    import random
    random.seed(0)
    lambda_total = 0.005
    t, T = _synthetic_decay(lambda_total, dt=5)
    T_noisy = T * np.array([1 + random.uniform(-0.03, 0.03) for _ in t])
    fit = compute_effective_eACH(t, T_noisy, ventilation_ach=3.0)
    assert fit["ci95_eACH_per_hr"] is not None
    lo, hi = fit["ci95_eACH_per_hr"]
    assert lo < fit["eACH_uv_effective"] < hi
    assert (hi - lo) > 0.01  # a real, non-degenerate width, not a near-zero point


def test_fit_effective_decay_rate_ci_matches_ols_formula():
    # Verify against the textbook closed-form OLS slope variance directly
    # (Var(slope) = residual_variance / sum((t-t_mean)^2)), independent of
    # this module's own implementation, rather than trusting one random
    # draw's coverage.
    import random
    random.seed(1)
    lambda_total = 0.004
    t, T = _synthetic_decay(lambda_total, dt=5)
    T_noisy = T * np.array([1 + random.uniform(-0.02, 0.02) for _ in t])
    fit = compute_effective_eACH(t, T_noisy, ventilation_ach=3.0)

    y = np.log(T_noisy)
    slope, intercept = np.polyfit(t, y, 1)
    resid = y - (slope * t + intercept)
    dof = len(t) - 2
    resid_var = np.sum(resid ** 2) / dof
    t_spread = np.sum((t - t.mean()) ** 2)
    expected_se_per_s = np.sqrt(resid_var / t_spread)
    assert math.isclose(fit["se_per_s"], expected_se_per_s, rel_tol=1e-9)


def test_compute_effective_eACH_propagates_ventilation_baseline_uncertainty():
    # Passing the control run's own SE/dof should widen the corrected
    # eACH's CI relative to treating the measured baseline as exact - the
    # whole point of combining two independent fits' uncertainty in
    # quadrature instead of just shifting a single-fit CI.
    import random
    random.seed(2)
    lambda_total = 0.005
    t, T = _synthetic_decay(lambda_total, dt=5)
    T_noisy = T * np.array([1 + random.uniform(-0.02, 0.02) for _ in t])
    measured_lambda = 2.67 / 3600.0

    fit_exact_baseline = compute_effective_eACH(
        t, T_noisy, ventilation_ach=3.0, ventilation_lambda_per_s=measured_lambda)
    fit_propagated = compute_effective_eACH(
        t, T_noisy, ventilation_ach=3.0, ventilation_lambda_per_s=measured_lambda,
        ventilation_lambda_se_per_s=5e-6, ventilation_lambda_dof=50)

    lo_exact, hi_exact = fit_exact_baseline["ci95_eACH_per_hr"]
    lo_prop, hi_prop = fit_propagated["ci95_eACH_per_hr"]
    assert (hi_prop - lo_prop) > (hi_exact - lo_exact)
    # Point estimate itself is unaffected - only the interval widens.
    assert math.isclose(fit_exact_baseline["eACH_uv_effective"], fit_propagated["eACH_uv_effective"])


def test_write_results_summary_adds_corrected_fields_only_when_measured_given(tmp_path):
    case_dir = tmp_path / "case"
    (case_dir / "postProcessing" / "volAverageLive1" / "0").mkdir(parents=True)
    t, T = _synthetic_decay(0.005)
    dat_path = case_dir / "postProcessing" / "volAverageLive1" / "0" / "volFieldValue.dat"
    with open(dat_path, "w") as f:
        f.write("# Region\n# Cells\n# Volume\n# Time\tvolAverage(T)\n")
        for ti, Ti in zip(t, T):
            f.write(f"{ti}\t{Ti}\n")

    out_path = tmp_path / "results.json"
    result_no_control = write_results_summary(str(case_dir), str(out_path), 3.0, 15.0)
    assert "eACH_uv_effective_corrected" not in result_no_control
    assert "mixing_efficiency_corrected" not in result_no_control

    result_with_control = write_results_summary(
        str(case_dir), str(out_path), 3.0, 15.0, measured_ventilation_ach=2.67)
    assert result_with_control["ventilation_ach_measured"] == 2.67
    assert "eACH_uv_effective_corrected" in result_with_control
    assert "mixing_efficiency_corrected" in result_with_control
    assert result_with_control["eACH_uv_effective_corrected"] > result_with_control["eACH_uv_effective"]

    with open(out_path) as f:
        saved = json.load(f)
    assert saved["ventilation_ach_measured"] == 2.67


def test_mechanical_mixing_efficiency_pct_ratio_of_measured_removal_to_delivered_flow():
    # Distinct from UV-specific mixing_efficiency: this compares the
    # measured EFFECTIVE removal rate against the measured DELIVERED flow
    # rate (run_pipeline.check_ach_delivery's own measured_ach) - a purely
    # mechanical, UV-independent question. Short-circuiting (air flowing
    # inlet-to-outlet without sweeping the room) would show up as
    # ventilation_ach_measured reading below the delivered flow rate.
    result = {"ventilation_ach_measured": 3.75, "ach_delivery": {"measured_ach": 6.0}}
    assert math.isclose(mechanical_mixing_efficiency_pct(result), 62.5)


def test_mechanical_mixing_efficiency_pct_none_when_either_input_missing():
    assert mechanical_mixing_efficiency_pct({}) is None
    assert mechanical_mixing_efficiency_pct({"ventilation_ach_measured": 3.75}) is None
    assert mechanical_mixing_efficiency_pct({"ach_delivery": {"measured_ach": 6.0}}) is None
    assert mechanical_mixing_efficiency_pct({"ventilation_ach_measured": 3.75, "ach_delivery": {}}) is None


def test_write_results_summary_stores_mechanical_mixing_efficiency(tmp_path):
    case_dir = tmp_path / "case"
    (case_dir / "postProcessing" / "volAverageLive1" / "0").mkdir(parents=True)
    t, T = _synthetic_decay(0.005)
    dat_path = case_dir / "postProcessing" / "volAverageLive1" / "0" / "volFieldValue.dat"
    with open(dat_path, "w") as f:
        f.write("# Region\n# Cells\n# Volume\n# Time\tvolAverage(T)\n")
        for ti, Ti in zip(t, T):
            f.write(f"{ti}\t{Ti}\n")

    out_path = tmp_path / "results.json"
    result = write_results_summary(
        str(case_dir), str(out_path), 3.0, 15.0,
        extra={"ach_delivery": {"measured_ach": 2.9}},
        measured_ventilation_ach=2.67,
    )
    assert result["mechanical_mixing_efficiency_pct"] == mechanical_mixing_efficiency_pct(result)
    with open(out_path) as f:
        saved = json.load(f)
    assert saved["mechanical_mixing_efficiency_pct"] == result["mechanical_mixing_efficiency_pct"]


def test_spatial_cov_zero_for_perfectly_uniform_field():
    assert spatial_coefficient_of_variation([1.0, 1.0, 1.0, 1.0]) == 0.0


def test_spatial_cov_recovers_known_ratio():
    # std/mean of [8, 9, 10, 11, 12] (mean=10, population std=sqrt(2)) -
    # a simple, hand-checkable case rather than just re-deriving the
    # formula the function itself uses.
    values = [8.0, 9.0, 10.0, 11.0, 12.0]
    expected = math.sqrt(2) / 10.0
    assert math.isclose(spatial_coefficient_of_variation(values), expected, rel_tol=1e-9)


def test_spatial_cov_none_for_zero_mean():
    assert spatial_coefficient_of_variation([-1.0, 0.0, 1.0]) is None


def test_windowed_stats_flat_series_has_zero_cv():
    t = list(range(100))
    T = [5.0] * 100
    mean, std, cv, n, span = windowed_stats(t, T, frac=0.15)
    assert mean == 5.0
    assert std == 0.0
    assert cv == 0.0
    assert n == 15
    assert span == t[-1] - t[-15]


def test_windowed_stats_only_uses_trailing_fraction():
    t = list(range(100))
    T = [0.0] * 80 + [10.0] * 20  # a jump partway through
    mean, std, cv, n, span = windowed_stats(t, T, frac=0.15)
    assert n == 15
    # last 15 samples are all in the post-jump plateau, so the mean should
    # reflect that plateau, not be dragged down by the earlier zeros.
    assert mean == 10.0
    assert std == 0.0


def test_windowed_stats_noisy_plateau_estimates_true_mean():
    import random
    random.seed(0)
    t = list(range(1000))
    true_mean = 0.3
    T = [true_mean + random.uniform(-0.05, 0.05) for _ in t]
    mean, std, cv, n, span = windowed_stats(t, T, frac=0.15)
    assert n == 150
    assert abs(mean - true_mean) < 0.01
    assert std > 0
    assert cv is not None and cv > 0


def test_windowed_stats_short_series_floors_at_two_points():
    t = [0, 1, 2]
    T = [1.0, 2.0, 3.0]
    mean, std, cv, n, span = windowed_stats(t, T, frac=0.15)
    assert n == 2  # round(3 * 0.15) = 0, floored to 2
    assert mean == 2.5  # mean of last 2 points
    assert span == t[-1] - t[-2]


def test_windowed_stats_cv_is_none_for_zero_mean():
    t = [0, 1, 2, 3]
    T = [1.0, -1.0, 1.0, -1.0]
    mean, std, cv, n, span = windowed_stats(t, T, frac=1.0)
    assert mean == 0.0
    assert cv is None


def test_check_plateau_windowed_flat_series_is_plateaued():
    t = list(range(100))
    T = [5.0] * 100
    converged, cv = check_plateau_windowed(t, T, frac=0.15, rel_tol=0.01)
    assert converged is True
    assert cv == 0.0


def test_check_plateau_windowed_still_rising_is_not_plateaued():
    # A monotonically rising series - the trailing window still has real
    # spread (not yet settled), so CV should exceed a strict tolerance.
    t = list(range(100))
    T = [float(i) for i in t]
    converged, cv = check_plateau_windowed(t, T, frac=0.15, rel_tol=0.01)
    assert converged is False
    assert cv > 0.01


def test_check_plateau_windowed_matches_reported_t_ss_statistic():
    # This is the whole point of the fix: the same statistic (windowed CV)
    # must drive both the "plateaued" verdict and the actual reported
    # T_ss/T_ss_cv - unlike the old sparse 5-sample-spread check, which
    # could disagree with the reported value entirely.
    import random
    random.seed(1)
    t = list(range(1000))
    T = [1.94 + random.uniform(-0.02, 0.02) for _ in t]  # tight plateau, ~1% noise
    mean, std, cv_reported, n, span = windowed_stats(t, T, frac=0.15)
    converged, cv_checked = check_plateau_windowed(t, T, frac=0.15, rel_tol=0.05)
    assert cv_checked == cv_reported
    assert converged is True


def test_check_plateau_windowed_none_cv_never_converges():
    # Zero-mean trailing window (cv is None from windowed_stats) must not
    # be silently treated as "plateaued".
    t = [0, 1, 2, 3]
    T = [1.0, -1.0, 1.0, -1.0]
    converged, cv = check_plateau_windowed(t, T, frac=1.0, rel_tol=0.01)
    assert converged is False
    assert cv is None


def test_check_plateau_windowed_negative_mean_never_converges():
    # Regression test for a real, confirmed incident: a diverged run whose
    # windowed mean went deeply negative (T_ss ~ -1.5e+79) produced a
    # NEGATIVE cv (std/mean, std always >= 0) - the old "cv <= rel_tol"
    # check never verified cv was non-negative, so a negative number
    # trivially satisfied it regardless of how huge the actual swing was,
    # silently marking a completely diverged field as converged=True and
    # propagating garbage (reduction_pct > 1e81%) into results.json.
    t = list(range(100))
    T = [-1.0e79, -1.02e79, -0.98e79, -1.01e79] * 25  # deeply negative, "tight"-looking relative spread
    converged, cv = check_plateau_windowed(t, T, frac=1.0, rel_tol=0.01)
    assert cv is not None and cv < 0  # confirms the mechanism: cv really is negative here
    assert converged is False


def test_windowed_stats_detrended_flat_series_matches_raw():
    t = list(range(100))
    T = [5.0] * 100
    mean, std, cv, n, span = windowed_stats_detrended(t, T, frac=0.15)
    assert mean == 5.0
    assert std == 0.0
    assert cv == 0.0


def test_windowed_stats_detrended_removes_a_pure_linear_trend():
    # A perfectly linear ramp has zero noise once detrended, even though
    # its raw std/CV (spread around the mean) is large.
    t = list(range(100))
    T = [float(i) for i in t]
    mean_raw, std_raw, cv_raw, _, _ = windowed_stats(t, T, frac=0.15)
    mean_det, std_det, cv_det, _, _ = windowed_stats_detrended(t, T, frac=0.15)
    assert mean_raw == mean_det  # mean itself is unaffected by detrending
    assert std_det < std_raw / 100  # essentially zero vs. a real raw spread
    assert cv_det < cv_raw / 100


def test_windowed_stats_detrended_noise_plus_trend_isolates_the_noise():
    # Real-run-like case: a window that's both slowly rising AND noisy.
    # Detrended CV should be much smaller than raw CV, since most of the
    # raw spread is the (real, but non-noise) drift, not fluctuation.
    import random
    random.seed(2)
    t = list(range(1000))
    T = [1.0 + 0.0005 * i + random.uniform(-0.002, 0.002) for i in t]
    _, std_raw, cv_raw, _, _ = windowed_stats(t, T, frac=0.15)
    _, std_det, cv_det, _, _ = windowed_stats_detrended(t, T, frac=0.15)
    assert cv_det < cv_raw
    assert std_det > 0  # real noise still present, not zeroed out entirely


def test_windowed_stats_detrended_falls_back_to_raw_for_two_points():
    t = [0, 1, 2]
    T = [1.0, 2.0, 3.0]
    mean, std, cv, n, span = windowed_stats_detrended(t, T, frac=0.15)
    assert n == 2
    assert mean == 2.5
    assert std > 0  # can't detrend 2 points meaningfully - falls back to raw


def test_fit_asymptotic_value_recovers_known_exponential_approach():
    # Synthetic data with a KNOWN true asymptote - the fit should recover
    # it closely, and the last raw sample should visibly undershoot it
    # (the whole point of extrapolating rather than just reading/averaging
    # the tail).
    true_Tinf, true_A, true_tau = 2.0, 0.5, 200.0
    t = np.arange(0, 1000, 2, dtype=float)
    T = true_Tinf - true_A * np.exp(-t / true_tau)
    result = fit_asymptotic_value(t, T)
    assert result is not None
    assert abs(result["Tinf"] - true_Tinf) < 0.01
    assert abs(result["tau"] - true_tau) / true_tau < 0.05
    assert T[-1] < result["Tinf"]  # last sample still below the true asymptote


def test_fit_asymptotic_value_none_for_too_little_data():
    result = fit_asymptotic_value([0, 1, 2], [1.0, 1.5, 1.8])
    assert result is None


def test_fit_asymptotic_value_rejects_a_negative_tinf():
    # Regression test for the same class of bug as
    # test_check_plateau_windowed_negative_mean_never_converges: fit_std is
    # always >= 0, so fit_cv = fit_std/Tinf goes negative whenever Tinf
    # does - every downstream "fit_cv > t_inf_rel_tol" rejection check in
    # steady_state_pipeline.py would then wrongly treat a negative fit_cv
    # as "well within tolerance" instead of catching an unphysical
    # (negative) extrapolated value. Fit data that actually approaches a
    # negative asymptote, and confirm it's rejected outright (None) rather
    # than returned with a misleadingly negative fit_cv.
    true_Tinf, true_A, true_tau = -3.0, 0.5, 200.0
    t = np.arange(0, 1000, 2, dtype=float)
    T = true_Tinf - true_A * np.exp(-t / true_tau)
    result = fit_asymptotic_value(t, T)
    assert result is None


def test_fit_asymptotic_value_none_for_pure_noise():
    # No underlying exponential shape at all - the fit shouldn't fabricate
    # a confident extrapolation from noise.
    import random
    random.seed(3)
    t = list(range(200))
    T = [random.uniform(-1, 1) for _ in t]
    result = fit_asymptotic_value(t, T)
    # Either fails to converge (None) or, if it does converge, the fit
    # quality must be poor (large fit_cv) rather than falsely confident.
    if result is not None:
        assert result["fit_cv"] is None or abs(result["fit_cv"]) > 0.5


def test_t_infinity_stability_needs_full_streak():
    assert check_t_infinity_stability([2.0, 2.0], rel_tol=0.02, streak=3) is False


def test_t_infinity_stability_true_for_tight_agreement():
    history = [1.6, 3.6, 2.5, 2.11, 1.99, 1.99, 2.02]
    assert check_t_infinity_stability(history, rel_tol=0.02, streak=3) is True


def test_t_infinity_stability_false_while_still_moving():
    history = [1.6, 3.6, 2.5, 2.11]
    assert check_t_infinity_stability(history, rel_tol=0.02, streak=3) is False


def test_t_infinity_stability_none_in_recent_window_blocks_stop():
    # A single failed fit within the trailing window must reset the streak
    # - can't confirm stability from a gap in the data.
    history = [2.0, 2.0, None, 2.0]
    assert check_t_infinity_stability(history, rel_tol=0.02, streak=3) is False


def test_t_infinity_stability_ignores_older_history_outside_streak():
    # A wildly different value earlier in history shouldn't block a stop
    # once the most recent `streak` values have settled.
    history = [100.0, 2.0, 2.0, 2.0]
    assert check_t_infinity_stability(history, rel_tol=0.02, streak=3) is True


def test_t_infinity_stability_respects_custom_streak():
    history = [2.0, 2.0]
    assert check_t_infinity_stability(history, rel_tol=0.02, streak=2) is True


def test_t_infinity_stability_zero_mean_never_stable():
    history = [1.0, -1.0, 0.0]
    assert check_t_infinity_stability(history, rel_tol=0.02, streak=3) is False
