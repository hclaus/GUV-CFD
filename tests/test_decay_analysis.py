import json
import math

import numpy as np

from guvcfd.decay_analysis import compute_effective_eACH, windowed_stats, write_results_summary, check_plateau_windowed


def _synthetic_decay(lambda_per_s, t_max=1000, dt=10, T0=1.0):
    t = np.arange(0, t_max + dt, dt, dtype=float)
    T = T0 * np.exp(-lambda_per_s * t)
    return t, T


def test_nominal_baseline_matches_uncorrected_behavior():
    lambda_total = 0.005  # /s, e.g. ventilation(3/hr) + UV(~15/hr) combined
    t, T = _synthetic_decay(lambda_total)
    eACH_eff, lambda_eff, _ = compute_effective_eACH(t, T, ventilation_ach=3.0)
    assert math.isclose(lambda_eff, lambda_total, rel_tol=1e-6)
    expected_eACH = (lambda_total - 3.0 / 3600.0) * 3600.0
    assert math.isclose(eACH_eff, expected_eACH, rel_tol=1e-6)


def test_measured_ventilation_baseline_overrides_nominal():
    lambda_total = 0.005
    t, T = _synthetic_decay(lambda_total)
    # Measured ventilation-only rate corresponds to 2.67/hr, not the nominal 3.0/hr.
    measured_lambda = 2.67 / 3600.0
    eACH_eff, _, _ = compute_effective_eACH(
        t, T, ventilation_ach=3.0, ventilation_lambda_per_s=measured_lambda)
    expected_eACH = (lambda_total - measured_lambda) * 3600.0
    assert math.isclose(eACH_eff, expected_eACH, rel_tol=1e-6)
    # Using the smaller measured baseline attributes MORE of the decay to UV.
    eACH_eff_nominal, _, _ = compute_effective_eACH(t, T, ventilation_ach=3.0)
    assert eACH_eff > eACH_eff_nominal


def test_write_results_summary_adds_corrected_fields_only_when_measured_given(tmp_path):
    case_dir = tmp_path / "case"
    (case_dir / "postProcessing" / "volAverage1" / "0").mkdir(parents=True)
    t, T = _synthetic_decay(0.005)
    dat_path = case_dir / "postProcessing" / "volAverage1" / "0" / "volFieldValue.dat"
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
