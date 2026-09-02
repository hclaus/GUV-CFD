"""The flow-convergence acceptance criterion.

Acceptance is judged on a WINDOW of chunk values, never on the change between
two neighbouring chunks. A neighbour-to-neighbour ("streak") test has two
independent ways of accepting a field that has not converged, and BOTH were
observed on real runs of this project:

  1. TURNING POINTS - |x_n - x_(n-1)| is smallest exactly where the slope
     crosses zero. patient ward v9 and v10 were each accepted off a single
     lucky chunk (1 of 15 and 1 of 7), landing on an extreme of their own
     series: v10 on its maximum, +24% off the series mean.

  2. SLOW DRIFT - every step small, total movement unbounded. The fan-free
     v9 variant produced three consecutive changes of 0.38/0.31/0.28% while
     still climbing, then excursed 10.7% two chunks later, 12.8% away from
     the value a streak test would have frozen.

Requiring a LONGER streak fixes neither: a slower drift satisfies a longer
streak, and a turning point is unaffected by how many chunks are demanded
around it. These tests pin both failure modes against the real series.
"""
import pytest

from guvcfd.run_pipeline import (_is_stable_oscillation, _oscillation_diagnostic,
                                  mean_is_stationary, stationarity_ratio,
                                  window_mean_settled)

# Real flow_convergence_history.json values, 500-iteration chunks.
V10 = [0.0155186, 0.0127752, 0.0207115, 0.0158107, 0.0164664, 0.0186372, 0.0223437, 0.0223738]
V9 = [0.019887, 0.0234816, 0.0190345, 0.0192873, 0.0228815, 0.0171836, 0.0195417, 0.0240796,
      0.0208312, 0.0203138, 0.0240453, 0.0236218, 0.020664, 0.0268301, 0.0174469, 0.0172849]
# Real volAverage|U| from the fan-free v9 variant - the slow-drift case.
C_NOFAN_MAGU = [0.0196678, 0.0197747, 0.0205494, 0.0206285, 0.0206935, 0.0207516,
                0.0202576, 0.018096, 0.0196773, 0.0194942, 0.0202165, 0.0196486,
                0.0206155, 0.0210959, 0.0200025, 0.0198235]
WINDOW, TOL = 6, 0.01


def _streak(values, need=3, tol=TOL):
    """The OLD criterion, kept only so the tests can show what it accepted."""
    run = 0
    for i in range(1, len(values)):
        run = run + 1 if abs(values[i] - values[i-1]) / abs(values[i-1]) <= tol else 0
        if run >= need:
            return i + 1
    return None


def _first(values, fn):
    for i in range(1, len(values) + 1):
        if fn(values[:i]):
            return i
    return None


# ---------------------------------------------------------------- turning points
@pytest.mark.parametrize("name,values", [("V10", V10), ("v9", V9)])
def test_real_histories_are_not_accepted_as_settled(name, values):
    assert _first(values, lambda v: window_mean_settled(v, WINDOW, TOL)) is None, name


@pytest.mark.parametrize("name,values", [("V10", V10), ("v9", V9)])
def test_the_old_rule_stopped_at_an_extreme_of_the_series(name, values):
    """Why a one-sample test is biased, not merely weak: it fires at turning
    points, so the frozen field is an extreme of the cycle."""
    rank = sorted(values).index(values[-1])
    assert rank <= 1 or rank >= len(values) - 2, f"{name}: rank {rank} of {len(values)}"


# --------------------------------------------------------------------- slow drift
def test_streak_accepts_a_steady_drift_but_the_window_test_does_not():
    """A +0.4%/chunk ramp: every neighbour change is under 1%, yet the series
    walks away without bound. This is the failure a longer streak cannot fix."""
    drift = [0.020 * (1.004 ** i) for i in range(14)]
    assert _streak(drift) is not None            # the old rule accepts it
    assert _first(drift, lambda v: window_mean_settled(v, WINDOW, TOL)) is None
    assert _first(drift, lambda v: mean_is_stationary(v, WINDOW)) is None


def test_the_real_fan_free_drift_is_not_accepted_early():
    """C_no_fan: streak fires at chunk 6, two chunks before a 10.7% excursion."""
    assert _streak(C_NOFAN_MAGU) == 6
    assert window_mean_settled(C_NOFAN_MAGU[:6], WINDOW, TOL) is False
    # 12.8% away from where the streak would have frozen it
    assert abs(C_NOFAN_MAGU[7] - C_NOFAN_MAGU[5]) / C_NOFAN_MAGU[5] > 0.12


# ------------------------------------------------------------ genuine convergence
def test_a_genuinely_settling_flow_is_still_accepted():
    """The fix must not make a real plateau unconvergeable."""
    clean = [0.05, 0.03, 0.022, 0.0205, 0.02012, 0.020030, 0.0200075,
             0.0200019, 0.0200005, 0.0200001, 0.02, 0.02]
    assert _first(clean, lambda v: window_mean_settled(v, WINDOW, TOL)) is not None


def test_stationary_mean_needs_two_windows_of_evidence():
    assert mean_is_stationary([1.0] * (2 * WINDOW - 1), WINDOW) is False
    assert mean_is_stationary([1.0] * (2 * WINDOW), WINDOW) is True


def test_growing_amplitude_is_rejected_even_when_the_mean_is_stationary():
    """A symmetric divergence keeps the mean put while the swing grows - which
    is why the amplitude check is kept alongside the stationarity test."""
    older = [1.0, 1.1, 1.0, 0.9, 1.0, 1.1]
    newer = [1.0, 2.0, 1.0, 0.0, 1.0, 2.0]        # same mean, 10x the amplitude
    assert mean_is_stationary(older + newer, WINDOW) is True
    assert _is_stable_oscillation(older + newer, WINDOW, 1.5) is False


def test_v9_reads_as_a_bounded_oscillation_once_there_is_enough_history():
    """v9's 16 chunks are stationary around ~0.021 - turbulent, not broken."""
    assert _is_stable_oscillation(V9, WINDOW, 1.5) is True
    assert mean_is_stationary(V9, WINDOW) is True


def test_v10_has_too_little_history_for_any_verdict():
    """8 chunks < 2*window, so nothing can be concluded - and the old rule
    nonetheless reported 'converged'."""
    assert _is_stable_oscillation(V10, WINDOW, 1.5) is False
    assert mean_is_stationary(V10, WINDOW) is False


# ------------------------------------------------------------------- diagnostic
def test_diagnostic_reports_the_criteria_actually_used():
    diag = _oscillation_diagnostic(
        [{"iteration": (i + 1) * 500, "value": v} for i, v in enumerate(V10)],
        window=WINDOW, growth_tol=1.5, rel_tol=TOL, n_iterations=500, check_field="p")
    assert diag["window_spread_pct"] is not None
    assert diag["window_spread_target_pct"] == pytest.approx(1.0)
    assert diag["drift_over_sem"] is None          # 8 chunks < 2*window
    assert diag["window_spread_pct"] > 1.0         # nowhere near settled


def test_stationarity_ratio_matches_the_acceptance_threshold():
    assert stationarity_ratio(V9, WINDOW) < 2.0
    assert mean_is_stationary(V9, WINDOW) is True
