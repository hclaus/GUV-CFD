import json
import math

import numpy as np

from guvcfd.decay_analysis import compute_effective_eACH, write_results_summary


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
