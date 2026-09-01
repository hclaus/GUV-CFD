"""A decay that was extended to reach its target must be fitted on its WHOLE
curve, not just the first leg.

run_decay_to_target restarts the solver from latestTime, and OpenFOAM then
opens postProcessing/<monitor>/<restart time>/ while the original "0" file
stays frozen at the provisional first duration. Every reader used to be
hardcoded to "0", so the reported rate came from exactly the too-short run
the extension existed to replace. On patient ward 4B1 v10 the 0/ leg is
500 s / 50.77% reduction (5.064 /hr) while the real curve runs to 5590 s
and 99.97% (5.129 /hr).
"""
import pytest

from guvcfd.decay_analysis import (decay_monitor_legs, read_decay_curve,
                                    fit_effective_decay_rate)

HEADER = "# Time  volAverage(T)\n"


def _leg(case, name, rows):
    d = case / "postProcessing" / "volAverageLive1" / name
    d.mkdir(parents=True)
    (d / "volFieldValue.dat").write_text(
        HEADER + "".join(f"{t}\t{v}\n" for t, v in rows))


def test_legs_are_listed_in_time_order_not_lexical(tmp_path):
    """'500' sorts before '4900' numerically but after it lexically."""
    for n in ("0", "500", "4900"):
        _leg(tmp_path, n, [(1.0, 1.0)])
    assert decay_monitor_legs(str(tmp_path)) == ["0", "500", "4900"]


def test_curve_concatenates_every_leg(tmp_path):
    _leg(tmp_path, "0", [(0.0, 1.0), (100.0, 0.5)])
    _leg(tmp_path, "100", [(100.0, 0.5), (200.0, 0.25)])
    t, T = read_decay_curve(str(tmp_path))
    assert list(t) == [0.0, 100.0, 200.0]      # restart point not duplicated
    assert list(T) == [1.0, 0.5, 0.25]


def test_single_leg_run_is_unchanged(tmp_path):
    """No extension happened - must behave exactly as before."""
    _leg(tmp_path, "0", [(0.0, 1.0), (50.0, 0.6), (100.0, 0.36)])
    t, T = read_decay_curve(str(tmp_path))
    assert len(t) == 3 and T[-1] == pytest.approx(0.36)


def test_truncating_to_the_first_leg_changes_the_fitted_rate(tmp_path):
    """The defect, in miniature: leg 0 alone must not reproduce the full
    curve's rate when the decay is not a single clean exponential."""
    import numpy as np
    slow = [(float(i), float(np.exp(-0.001 * i))) for i in range(0, 501, 50)]
    fast = [(float(i), float(np.exp(-0.001 * 500) * np.exp(-0.004 * (i - 500))))
            for i in range(550, 2001, 50)]
    _leg(tmp_path, "0", slow)
    _leg(tmp_path, "500", fast)
    t_all, T_all = read_decay_curve(str(tmp_path))
    lam_all = fit_effective_decay_rate(t_all, T_all)["lambda_per_s"]
    lam_first = fit_effective_decay_rate([r[0] for r in slow], [r[1] for r in slow])["lambda_per_s"]
    assert lam_first == pytest.approx(0.001, rel=1e-3)
    assert lam_all > lam_first * 2          # full curve is much faster


def test_empty_curve_raises_instead_of_returning_nothing(tmp_path):
    """A silently-empty curve would be fitted or divided by downstream."""
    (tmp_path / "postProcessing" / "volAverageLive1").mkdir(parents=True)
    with pytest.raises((OSError, RuntimeError)):
        read_decay_curve(str(tmp_path))
